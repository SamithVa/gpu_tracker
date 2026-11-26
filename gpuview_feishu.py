import argparse
import fcntl
import getpass
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, time as midnight_time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import requests

from gpuview_report import server_label, usage_totals


STATE = Path(__file__).resolve().parent / "state"
DATABASE = STATE / "usage.sqlite3"
CONFIG = STATE / "feishu.json"
API = "https://open.feishu.cn/open-apis"
FIELDS = {"Sync Key": 1, "Server": 1, "User": 1, "GPU user": 1, "Account": 1, "Usage date": 5,
          "GPU-hours": 2, "VRAM GiB-hours": 2, "Recording started": 5, "Updated at": 5}
KEY_PATTERN = re.compile(r"gpuview-daily-v1:[0-9a-f]{64}")
LEGACY_KEY_PATTERN = re.compile(r"gpuview-v1:[0-9a-f]{64}")
REPORT_ZONE = ZoneInfo("Asia/Shanghai")
HISTORY_DAYS = 31
FIRST_INTERVALS = """
    SELECT 'local', MIN(start) FROM intervals HAVING MIN(start) IS NOT NULL
    UNION ALL
    SELECT host, MIN(start) FROM remote_intervals GROUP BY host
"""


def table_target(url):
    parts = urlsplit(url)
    target = re.fullmatch(r"/(base|wiki)/([a-zA-Z0-9]+)/?", parts.path)
    table = parse_qs(parts.query).get("table", [])
    hostname = parts.hostname or ""
    if parts.scheme != "https" or not (hostname == "feishu.cn" or hostname.endswith(".feishu.cn")):
        raise ValueError("Use an HTTPS Feishu Base or Wiki URL")
    if not target or len(table) > 1 or (table and not re.fullmatch(r"tbl[a-zA-Z0-9]+", table[0])):
        raise ValueError("Use a /base/… or /wiki/… URL, optionally with ?table=tbl…")
    return target[1], target[2], table[0] if table else None


def configure(url):
    table_target(url)
    if not sys.stdin.isatty():
        raise ValueError("Run configuration in an interactive terminal")
    app_id = input("Feishu App ID: ").strip()
    app_secret = getpass.getpass("Feishu App Secret: ").strip()
    if not app_id or not app_secret:
        raise ValueError("Both Feishu credentials are required")
    with tempfile.NamedTemporaryFile(mode="w", dir=STATE, delete=False) as handle:
        temp = Path(handle.name)
        try:
            json.dump({"url": url, "app_id": app_id, "app_secret": app_secret}, handle)
            handle.flush()
            temp.replace(CONFIG)
        finally:
            temp.unlink(missing_ok=True)
    print("Saved Feishu configuration in state/feishu.json (owner-only).")


def load_config():
    try:
        fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ValueError("Configure the Feishu table and credentials first") from None
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Feishu configuration must be an owner-only regular file")
        config = json.load(source)
    if not isinstance(config, dict) or any(not isinstance(config.get(k), str) or not config[k]
                                          for k in ("url", "app_id", "app_secret")):
        raise ValueError("Feishu configuration is missing the URL or app credentials")
    aliases = config.get("server_names", {})
    if not isinstance(aliases, dict) or any(not isinstance(name, str) or not name.strip() for name in aliases.values()):
        raise ValueError("Feishu server_names must map server IDs to nonempty display names")
    return config


def day_bounds(now):
    today = datetime.fromtimestamp(now, REPORT_ZONE).date()
    first = today - timedelta(days=HISTORY_DAYS - 1)
    for offset in range(HISTORY_DAYS + 1):
        yield datetime.combine(first + timedelta(days=offset), midnight_time.min, REPORT_ZONE).timestamp()


def remember_recording_starts(database):
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS recording_started (
                host TEXT PRIMARY KEY,
                timestamp REAL NOT NULL
            )
        """)
        db.execute(f"""
            INSERT INTO recording_started {FIRST_INTERVALS}
            ON CONFLICT(host) DO UPDATE SET timestamp = excluded.timestamp
            WHERE excluded.timestamp < recording_started.timestamp
        """)


def report_rows(database, now, display_names=None, *, server_names=None):
    server_names = server_names or {}
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        if display_names is None:
            display_names = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'gpu_account_names'").fetchone():
                display_names = {(server, user): name for server, user, name in db.execute(
                    "SELECT server, username, chinese_name FROM gpu_account_names"
                )}
        starts = dict(db.execute(FIRST_INTERVALS))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'recording_started'").fetchone():
            for host, timestamp in db.execute("SELECT host, timestamp FROM recording_started"):
                starts[host] = min(starts.get(host, timestamp), timestamp)
        rows = {}
        bounds = list(day_bounds(now))
        for start, end in zip(bounds, bounds[1:]):
            if start >= now:
                break
            for host, user, hours, memory in usage_totals(db, start, until=min(end, now)):
                if user == "?":
                    continue
                day = int(start * 1000)
                identity = json.dumps([host, user, day], separators=(",", ":"))
                key = f"gpuview-daily-v1:{hashlib.sha256(identity.encode()).hexdigest()}"
                server = server_names.get(host) or server_label(host)
                name = display_names.get((server, user)) or user
                rows[key] = {"Sync Key": key, "Server": server, "User": user, "GPU user": name,
                             "Account": f"{name} · {server}", "Usage date": day,
                             "GPU-hours": round(hours, 6), "VRAM GiB-hours": round(memory, 6),
                             "Recording started": int(starts[host] * 1000),
                             "Updated at": int(now * 1000)}
    return rows


class Feishu:
    def __init__(self, session, config):
        self.session = session
        kind, base, table = table_target(config["url"])
        credentials = {key: config[key] for key in ("app_id", "app_secret")}
        auth = self.call("POST", "/auth/v3/tenant_access_token/internal", "token request", json=credentials)
        self.session.headers["Authorization"] = f"Bearer {auth['tenant_access_token']}"
        if kind == "wiki":
            result = self.call("GET", "/wiki/v2/spaces/get_node", "Wiki link lookup", params={"token": base})
            node = result["data"]["node"]
            if node.get("obj_type") != "bitable":
                raise ValueError("The Wiki link does not point to a Base. Open the Base table and copy its link.")
            base = node["obj_token"]
        if not isinstance(base, str) or not re.fullmatch(r"[a-zA-Z0-9]+", base):
            raise ValueError("Feishu returned an invalid Base ID")
        parent = f"/bitable/v1/apps/{base}"
        if table is None:
            tables = self.items("tables", parent=parent)
            only_table = next(tables, None)
            if only_table is None or next(tables, None) is not None:
                raise ValueError("Open the intended table and copy its URL with ?table=tbl…")
            table = only_table["table_id"]
        if not isinstance(table, str) or not re.fullmatch(r"tbl[a-zA-Z0-9]+", table):
            raise ValueError("Feishu returned an invalid table ID")
        self.table = f"{parent}/tables/{table}"

    def call(self, method, path, action, **kwargs):
        try:
            result = self.session.request(method, API + path, timeout=15, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise ValueError(f"Feishu {action} could not reach the API") from None
        try:
            data = result.json()
        except ValueError:
            raise ValueError(f"Feishu {action} returned HTTP {result.status_code}") from None
        code = data.get("code") if isinstance(data, dict) else None
        if result.status_code != 200 or code != 0:
            error_code = code if type(code) is int else "unknown"
            raise ValueError(f"Feishu {action} failed (HTTP {result.status_code}, code {error_code})")
        return data

    def items(self, collection, parent=None):
        params = {"page_size": 100}
        seen = set()
        path = f"{parent or self.table}/{collection}"
        while True:
            result = self.call("GET", path, f"list {collection}", params=params)["data"]
            yield from result.get("items") or []
            if not result.get("has_more"):
                return
            token = result.get("page_token")
            if not token or token in seen:
                raise ValueError(f"Feishu returned an invalid {collection} page token")
            seen.add(token)
            params["page_token"] = token

    def check_fields(self, create=False):
        fields = {item["field_name"]: item["type"] for item in self.items("fields")}
        for name, field_type in FIELDS.items():
            if name not in fields and create:
                self.call("POST", f"{self.table}/fields", "create field", json={"field_name": name, "type": field_type})
            elif fields.get(name) != field_type:
                raise ValueError(f"Feishu field {name!r} is missing or has the wrong type")

    def write_records(self, operation, records):
        for offset in range(0, len(records), 100):
            batch = records[offset:offset + 100]
            self.call("POST", f"{self.table}/records/batch_{operation}", f"batch {operation}", json={"records": batch})

    def roster_names(self, table_id):
        base, usage_table = self.table.rsplit("/tables/", 1)
        if not isinstance(table_id, str) or not re.fullmatch(r"tbl[a-zA-Z0-9]+", table_id) or table_id == usage_table:
            raise ValueError("Invalid Feishu roster table ID")
        names = {}
        for record in self.items("records", parent=f"{base}/tables/{table_id}"):
            fields = record.get("fields") or {}
            server, user, name = (text_value(fields.get(key)) for key in ("服务器", "Linux 账号", "中文姓名"))
            if not (isinstance(server, str) and server.strip() and isinstance(user, str) and user.strip()):
                continue
            identity = (server.strip(), user.strip())
            if identity in names:
                raise ValueError("The Feishu roster has duplicate entries for one server and Linux account")
            names[identity] = name.strip() if isinstance(name, str) else ""
        return names


def text_value(value):
    if isinstance(value, list) and all(isinstance(part, dict) for part in value):
        return "".join(part.get("text", "") for part in value)
    return value


def sync(feishu, rows, now):
    existing = {}
    legacy = []
    obsolete = []
    cutoff = next(day_bounds(now)) * 1000
    for record in feishu.items("records"):
        fields = record.get("fields") or {}
        key = text_value(fields.get("Sync Key"))
        if not isinstance(key, str):
            continue
        if LEGACY_KEY_PATTERN.fullmatch(key):
            legacy.append(record["record_id"])
            continue
        if not KEY_PATTERN.fullmatch(key):
            continue
        day = fields.get("Usage date")
        if text_value(fields.get("User")) == "?" or (isinstance(day, (int, float)) and day < cutoff):
            obsolete.append(record["record_id"])
            continue
        if key in existing:
            raise ValueError("The Feishu table contains duplicate gpuview sync keys")
        existing[key] = record

    updates = []
    creates = []
    for key, fields in rows.items():
        if key not in existing:
            creates.append({"fields": fields})
        elif any(text_value(existing[key]["fields"].get(name)) != value
                 for name, value in fields.items() if name != "Updated at"):
            updates.append({"record_id": existing[key]["record_id"], "fields": fields})
    feishu.write_records("update", updates)
    feishu.write_records("create", creates)
    if legacy:
        observed = (text_value((r.get("fields") or {}).get("Sync Key")) for r in feishu.items("records"))
        verified = {key for key in observed if isinstance(key, str)}
        if not rows or not rows.keys() <= verified:
            raise ValueError("Daily GPU usage must be verified before removing old period summaries")
    deleted = legacy + obsolete
    feishu.write_records("delete", deleted)
    return len(updates), len(creates), len(deleted)


def main():
    parser = argparse.ArgumentParser(description="Export gpuview usage summaries to Feishu Base")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--configure", metavar="TABLE_URL")
    actions.add_argument("--init-fields", action="store_true")
    actions.add_argument("--sync", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.configure:
        configure(args.configure)
        return
    if not args.init_fields and not args.sync:
        config = load_config() if CONFIG.exists() else {}
        rows = report_rows(DATABASE, time.time(), server_names=config.get("server_names"))
        print(json.dumps(list(rows.values()), ensure_ascii=False, indent=2))
        return
    with (STATE / "feishu.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A Feishu sync is already running") from None
        with requests.Session() as session:
            config = load_config()
            feishu = Feishu(session, config)
            feishu.check_fields(create=args.init_fields)
            if args.init_fields:
                print("Feishu Base fields are ready.")
                return
            remember_recording_starts(DATABASE)
            now = time.time()
            roster_table = config.get("roster_table")
            display_names = feishu.roster_names(roster_table) if roster_table else None
            rows = report_rows(DATABASE, now, display_names, server_names=config.get("server_names"))
            updated, created, deleted = sync(feishu, rows, now)
    print(f"Feishu sync completed: {created} created, {updated} updated, {deleted} removed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        if isinstance(error, (ValueError, sqlite3.Error)):
            sys.exit(str(error))
        sys.exit("Feishu export failed while reading configuration or API data")
