# GPU Tracker

[English README](README.md)

按 Linux 账号记录共享 NVIDIA GPU 服务器上的 GPU 占用时长和显存用量。中心端将采样存入 SQLite，提供私有 GPUView 仪表盘，并可将每日用量同步到飞书多维表格。

## 工作流程

本仓库包含**中心端**，包括 SSH 接收器、本机采集器、仪表盘和飞书同步脚本。发送端运行在另一台 GPU 服务器上，按下文的协议上传采样。

```mermaid
flowchart TD
    Remote["远程 GPU 服务器"] -->|"部署中每 30 秒采样"| Queue["发送端 SQLite 待上传队列"]
    Queue -->|"通过 SSH 批量发送 JSON，部署中约每 3 分钟"| Receiver["中心端 SSH 接收器 gpuview_ingest.py"]
    Receiver -->|"校验、去重并保存"| Database[("中心端 SQLite")]
    Local["中心端本机 GPU usage.py"] -->|"每 30 秒采样"| Database
    Database --> Dashboard["私有 GPUView 仪表盘"]
    Database -->|"按日汇总"| Exporter["飞书同步脚本 gpuview_feishu.py"]
    Exporter -->|"通过 HTTPS 同步，通常每 30 分钟"| Feishu["飞书多维表格和排行榜"]
```

## 1. 启动中心端

准备一台装有 Python 3.10 及以上版本、可运行 `nvidia-smi` 的 Linux 服务器。以普通用户安装依赖并启动中心端。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python usage.py
```

中心端保留约 31 天的采样记录，通过 `state/gpuview.sock` 提供 HTTP 服务。在自己的电脑上建立 SSH 隧道，将命令中的路径和登录信息替换为实际值。

```sh
ssh -N -L 127.0.0.1:9988:/absolute/path/to/gpu_tracker/state/gpuview.sock user@hub
```

打开 `http://127.0.0.1:9988/` 查看排行榜，或打开 `/live` 查看实时仪表盘。需要持续采集时，可用服务器现有的服务管理工具运行 `usage.py`。

## 2. 接入远程发送端

在中心端的 OpenSSH `authorized_keys` 中，为每台发送服务器添加独立公钥，通过 `restrict` 和固定命令将公钥绑定到唯一的发送端 ID。接收命令以 SQLite 数据库所属的账号运行。替换绝对路径、`sender-id` 和示例公钥。

```text
restrict,command="/usr/bin/python3 -I /absolute/path/to/gpu_tracker/gpuview_ingest.py sender-id" ssh-ed25519 <sender-public-key>
```

发送端通过 SSH 标准输入提交版本为 `1` 的 JSON。每批最多 100 个采样区间，最大 512 KiB。区间包含 Unix 秒时间戳 `start_utc` 和 `end_utc`，以及按用户汇总的 `username`、`gpus` 和 `memory_mib`。空闲时使用 `users: []`，无法查到进程所属账号时使用 `?`。

实际上传时使用近期的采样数据，并校验中心端的 SSH 主机密钥。接收器在提交整批数据后返回退出码 `0`。重复发送相同批次不会重复计入。发送端收到成功结果后再推进上传进度。[JSON 示例](README.md#enroll-a-remote-sender)

## 3. 同步到飞书

给飞书企业自建应用单独授予目标多维表格的编辑权限。在中心端目录下交互式配置应用、初始化字段，并测试同步。

```sh
.venv/bin/python gpuview_feishu.py --configure 'https://your-team.feishu.cn/base/BASE_TOKEN?table=tblTABLE'
.venv/bin/python gpuview_feishu.py --init-fields
.venv/bin/python gpuview_feishu.py --sync
```

定时任务可使用解释器和脚本的绝对路径，每 30 分钟运行一次 `--sync`。同步以北京时间按服务器、Linux 账号和日期写入每日用量。飞书用这些日记录计算不同日期范围的排行。GPU 小时衡量 GPU 被进程占用的时间，也包含持有 GPU 上下文的空闲时间。

`state/feishu.json` 仅供当前用户读取。可选配置 `server_names` 将 `local` 和远程发送端 ID 映射为展示名称。`roster_table` 指定含有 `服务器`、`Linux 账号` 和 `中文姓名` 三列的名册表。名册中的服务器名称应与同步使用的展示名称一致。
