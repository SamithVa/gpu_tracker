import csv
import fcntl
import logging
import os
import socket
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

import psutil
from bottle import request, response, template
from gpuview import core
from gpuview.app import app, index

from .ingest import DATABASE, RETENTION, SCHEMA
from .reporting import server_label, usage_totals


TEMPLATES = Path(__file__).resolve().parent / "templates"
STATE = DATABASE.parent
SOCKET = STATE / "gpuview.sock"
SAMPLE_SECONDS = 30
DAY = 24 * 60 * 60


def snapshot():
    result = subprocess.run(
        ["/usr/bin/nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    devices = {}
    memory = Counter()
    owners = {}
    for gpu, pid_text, mib in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
        pid = int(pid_text)
        if pid not in owners:
            try:
                owners[pid] = psutil.Process(pid).username()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                owners[pid] = "?"
        user = owners[pid]
        devices.setdefault(user, set()).add(gpu)
        if mib.isdecimal():
            memory[user] += int(mib)
    return [(user, len(gpus), memory[user]) for user, gpus in devices.items()]


def collect():
    previous = None
    with closing(sqlite3.connect(DATABASE)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        while True:
            started = time.monotonic()
            try:
                users = snapshot()
                now = time.time()
                with db:
                    if previous is not None:
                        last_sample, last_users = previous
                        if 0 < now - last_sample <= SAMPLE_SECONDS * 1.5:
                            db.execute("INSERT INTO intervals VALUES (?, ?)", (last_sample, now))
                            db.executemany(
                                "INSERT INTO usage VALUES (?, ?, ?, ?)",
                                ((last_sample, *usage) for usage in last_users),
                            )
                    db.execute("INSERT OR REPLACE INTO sampled VALUES (1, ?)", (now,))
                    db.execute("DELETE FROM intervals WHERE start < ?", (now - RETENTION,))
                    db.execute("DELETE FROM remote_intervals WHERE end < ?", (now - RETENTION,))
                previous = now, users
            except Exception:
                logging.exception("Could not record GPU usage")
                previous = None
            time.sleep(max(0, SAMPLE_SECONDS - (time.monotonic() - started)))


@app.get("/")
@app.get("/usage")
def usage_page():
    durations = {"24h": DAY, "7d": 7 * DAY, "30d": 30 * DAY}
    period = request.query.get("period", "7d")
    if period not in durations:
        period = "7d"
    host = request.query.get("host", "all")
    since = time.time() - durations[period]
    with closing(sqlite3.connect(DATABASE)) as db:
        db.execute("BEGIN")
        sources = dict(db.execute(
            """
            SELECT 'local', (SELECT timestamp FROM sampled WHERE id = 1)
            UNION ALL
            SELECT host, MAX(end) FROM remote_intervals GROUP BY host ORDER BY 1
            """
        ))
        if host != "all" and host not in sources:
            host = "all"
        host_labels = {name: server_label(name) for name in sources}
        timestamps = sources.values() if host == "all" else [sources[host]]
        last_sample = max((value for value in timestamps if value is not None), default=None)
        params = {"since": since, "host": host}
        covered = db.execute(
            """
            SELECT COALESCE(SUM(end - MAX(start, :since)), 0)
            FROM (
                SELECT start, end FROM intervals WHERE :host IN ('local', 'all') AND end > :since
                UNION ALL
                SELECT start, end FROM remote_intervals WHERE (:host = 'all' OR host = :host) AND end > :since
            )
            """,
            params,
        ).fetchone()[0]
        users = usage_totals(db, since, host)

    response.set_header("Cache-Control", "no-store")
    return template(
        "usage", template_lookup=[str(TEMPLATES)], period=period,
        host=host, hosts=host_labels, users=users, covered=covered,
        target_hours=durations[period] // 3600 * (len(sources) if host == "all" else 1),
        unit="server-hours" if host == "all" and len(sources) > 1 else "hours",
        stale=last_sample is None or time.time() - last_sample > (90 if len(sources) == 1 or host == "local" else 600),
        last_sample=datetime.fromtimestamp(last_sample).astimezone() if last_sample is not None else None,
    )


@app.get("/live")
def live_page():
    response.set_header("Cache-Control", "no-store")
    leaderboard_link = '<a class="btn btn-outline-light me-4" href="/">User rankings</a>'
    return index().replace("</nav>", f"{leaderboard_link}</nav>", 1)


class LocalRequestHandler(WSGIRequestHandler):
    def get_environ(self):
        self.client_address = ("localhost", 0)
        return super().get_environ()

    def log_message(self, format, *args):
        pass


class LocalServer(WSGIServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        self.socket.bind(self.server_address)
        self.server_name = "localhost"
        self.server_port = 80
        self.setup_environ()


def main():
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        serve()


def serve():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(STATE / "usage.log", maxBytes=1_000_000, backupCount=2)],
    )
    with closing(sqlite3.connect(DATABASE)) as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS intervals (start REAL PRIMARY KEY, end REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS intervals_end ON intervals(end);
            CREATE TABLE IF NOT EXISTS usage (
                start REAL REFERENCES intervals(start) ON DELETE CASCADE,
                username TEXT NOT NULL,
                gpus INTEGER NOT NULL,
                memory_mib INTEGER NOT NULL,
                PRIMARY KEY (start, username)
            );
            CREATE TABLE IF NOT EXISTS sampled (id INTEGER PRIMARY KEY, timestamp REAL NOT NULL);
            """
        )
        db.executescript(SCHEMA)
    core.safe_zone(True)
    SOCKET.unlink(missing_ok=True)
    with LocalServer(str(SOCKET), LocalRequestHandler) as server:
        server.set_app(app)
        threading.Thread(target=collect, name="gpu-usage", daemon=True).start()
        logging.info("Starting gpuview on %s", SOCKET)
        server.serve_forever()


if __name__ == "__main__":
    main()
