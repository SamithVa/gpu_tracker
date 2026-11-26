# 远程 GPU 发送端

[English setup guide](README.md)

在每台远程 NVIDIA GPU 服务器上运行发送端，以普通 Linux 账号采集各用户占用的 GPU 数量和显存，通过 SSH 上传到中心端。只需要 Python 3.10+ 标准库、`nvidia-smi`、OpenSSH 和用户级 systemd，不需要安装中心端依赖或配置飞书凭据。

## 目录

- `sender.py`：每 30 秒采集一次，写入本地 SQLite。
- `uploader.py`：持久化待发送批次，收到成功响应后推进游标。
- `install.py`：根据当前仓库、Python 和状态目录的绝对路径生成用户服务。
- `examples/ssh_config`：严格校验主机密钥的 SSH 配置模板。
- `tests/`：采集、上传、安装配置与真实中心端接收器兼容性测试。

## 本地采集

以普通用户在仓库根目录运行，不使用 sudo：

```sh
nvidia-smi --query-gpu=uuid,name --format=csv,noheader
python3 sender/sender.py probe
python3 sender/install.py collect
# 至少等 65 秒，检查完整区间持续增加。
python3 sender/sender.py status
```

核对 probe 中的 Linux 账号是否符合预期。无法解析的进程所有者归入 `?`。共享 GPU 的用户分别计入自己的 GPU 占用，显存按该用户的进程求和。持有 GPU 上下文的空闲进程也算占用，不等于 GPU 利用率。

默认状态目录为 `sender/state/`，权限为 0700。可通过 `--state-dir /绝对路径` 或 `GPUVIEW_STATE_DIR` 指定其他目录；采集、安装、验证和手动上传必须始终使用同一目录。数据库、私钥和日志仅当前账号可读写。只保存区间时间、用户名、GPU 数量和显存，不保存 PID、命令、作业名或环境变量。采集失败、重启或超过 45 秒的采样间隔不计入用量。

## SSH 接入和自动上传

生成专用密钥，已有密钥不要覆盖：

```sh
ssh-keygen -t ed25519 -N '' -f sender/state/id_ed25519 -C gpu-tracker-sender
cat sender/state/id_ed25519.pub
cp sender/examples/ssh_config sender/state/ssh_config
chmod 600 sender/state/ssh_config
```

私钥留在发送服务器上。将公钥和稳定的发送端 ID 交给中心端操作者，按[中心端接入说明](../README.zh-CN.md#2-接入远程发送端)安装 `restrict` 与固定接收命令。中心端账号必须有数据库写权限；独立低权限账号比个人账号隔离更强。

修改 `sender/state/ssh_config` 中的主机、端口、账号、私钥与 known_hosts 绝对路径，保留 `gpuview-hub` 别名。向中心端操作者取得可信的主机公钥与指纹，独立核对后写入 `sender/state/known_hosts` 并设为 0600。非默认端口的主机字段为 `[hostname]:port`。不要关闭主机密钥校验，也不要把未经验证的扫描结果当作可信来源。

接入完成后，用真实采样验证：

```sh
python3 sender/uploader.py verify
python3 sender/install.py uploads
python3 sender/uploader.py status
systemctl --user list-timers gpuview-upload.timer --all --no-pager
```

`verify` 原样发送同一批数据两次，两次都需返回退出码 0 且无输出。需要至少一个未上传区间，队列为空时等待采集即可。安装器在尚未验证时会先做验证，再上传积压并启用每 3 分钟运行的定时任务。用中心端仪表盘的 Server 筛选核对该服务器的覆盖时间和排名。

每批最多 100 个区间、512 KiB，时间戳须在过去 31 天内，最多领先中心端 60 秒。批次在发送前持久化，失败后保留并原样重试，确认提交后才推进游标。过期未发送数据保留并报错，不会自动丢弃；已发送历史也保留在本地，长期运行需关注磁盘容量。

## 运维与迁移

```sh
python3 sender/uploader.py upload
tail -n 30 sender/state/upload.log
systemctl --user status gpuview-sender.service
```

本地上传日志自动轮转。按服务器策略允许时执行 `loginctl enable-linger "$USER"`，使服务在注销后继续运行；不允许时联系管理员。

停止上传：`systemctl --user disable --now gpuview-upload.timer`；停止正在进行的上传：`systemctl --user stop gpuview-upload.service`；停止采集：`systemctl --user disable --now gpuview-sender.service`。

已有部署无需为提交代码而迁移。若要迁移，先停止旧采集与上传，保留原数据库、密钥和游标，将 SSH 配置放到原状态目录的 `ssh_config`，新安装器始终指定原状态目录。不要启动两个采集器。安装器不会覆盖符号链接指向的旧服务，需明确处理旧链接后再安装。保持安装后的仓库路径不变。

## 测试

从仓库根目录运行，无需 GPU、SSH 或第三方 Python 依赖：

```sh
python3 -m unittest discover -s sender/tests -t sender -v
```
