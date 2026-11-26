import json
import math
import re
import signal
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path


DATABASE = Path(__file__).resolve().parents[1] / "state" / "usage.sqlite3"
DAY = 86400
RETENTION = 31 * DAY
MAX_BYTES = 512 * 1024
MAX_INTERVALS = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS remote_intervals (
    host TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    PRIMARY KEY (host, start)
);
CREATE INDEX IF NOT EXISTS remote_intervals_end ON remote_intervals(end);
CREATE TABLE IF NOT EXISTS remote_usage (
    host TEXT NOT NULL,
    start REAL NOT NULL,
    username TEXT NOT NULL,
    gpus INTEGER NOT NULL,
    memory_mib INTEGER NOT NULL,
    PRIMARY KEY (host, start, username),
    FOREIGN KEY (host, start) REFERENCES remote_intervals(host, start) ON DELETE CASCADE
);
"""


def valid_timestamp(value, lower, upper):
    return type(value) in (int, float) and lower <= value <= upper and math.isfinite(value)


def parse_batch(payload, now):
    if len(payload) > MAX_BYTES:
        raise ValueError("Batch exceeds the byte limit")
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict) or data.keys() != {"version", "intervals"}:
        raise ValueError("Expected a version and intervals")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Unsupported protocol version")
    intervals = data["intervals"]
    if not isinstance(intervals, list) or not 1 <= len(intervals) <= MAX_INTERVALS:
        raise ValueError("Expected 1 to 100 intervals")

    batch = []
    for interval in intervals:
        if not isinstance(interval, dict) or interval.keys() != {"start_utc", "end_utc", "users"}:
            raise ValueError("Invalid interval fields")
        start, end = interval["start_utc"], interval["end_utc"]
        if not all(valid_timestamp(t, now - RETENTION, now + 60) for t in (start, end)):
            raise ValueError("Timestamp outside the accepted window")
        if not 0 < end - start <= 45:
            raise ValueError("Interval must last up to 45 seconds")
        rows = interval["users"]
        if not isinstance(rows, list) or len(rows) > 1024:
            raise ValueError("Invalid list of users")
        users = {}
        for row in rows:
            if not isinstance(row, dict) or row.keys() != {"username", "gpus", "memory_mib"}:
                raise ValueError("Invalid user fields")
            username, gpus, memory = row["username"], row["gpus"], row["memory_mib"]
            if not isinstance(username, str) or not 1 <= len(username) <= 128 or not username.isprintable():
                raise ValueError("Invalid username")
            if username in users:
                raise ValueError("Duplicate user in an interval")
            if type(gpus) is not int or not 1 <= gpus <= 1024:
                raise ValueError("Invalid GPU count")
            if type(memory) is not int or not 0 <= memory <= 2**30:
                raise ValueError("Invalid VRAM value")
            users[username] = (gpus, memory)
        batch.append((start, end, users))
    return batch


def store_batch(db, host, batch):
    with db:
        db.execute("BEGIN IMMEDIATE")
        for start, end, users in batch:
            existing = db.execute(
                "SELECT end FROM remote_intervals WHERE host = ? AND start = ?", (host, start)
            ).fetchone()
            if existing is not None:
                rows = db.execute(
                    "SELECT username, gpus, memory_mib FROM remote_usage WHERE host = ? AND start = ?",
                    (host, start),
                )
                if existing[0] != end or {name: (gpus, memory) for name, gpus, memory in rows} != users:
                    raise ValueError("Conflicting repeat of an interval")
                continue

            previous = db.execute(
                "SELECT end FROM remote_intervals WHERE host = ? AND start < ? ORDER BY start DESC LIMIT 1",
                (host, end),
            ).fetchone()
            if previous is not None and previous[0] > start:
                raise ValueError("Overlapping intervals for one sender")
            db.execute("INSERT INTO remote_intervals VALUES (?, ?, ?)", (host, start, end))
            db.executemany(
                "INSERT INTO remote_usage VALUES (?, ?, ?, ?, ?)",
                ((host, start, name, *values) for name, values in users.items()),
            )


def main(host):
    if host in {"all", "local"} or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", host):
        raise ValueError("Invalid sender ID")
    signal.alarm(30)
    payload = sys.stdin.buffer.read(MAX_BYTES + 1)
    signal.alarm(0)
    batch = parse_batch(payload, time.time())
    with closing(sqlite3.connect(f"{DATABASE.as_uri()}?mode=rw", uri=True)) as db:
        store_batch(db, host, batch)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected one sender ID")
        main(sys.argv[1])
    except (ValueError, RecursionError) as error:
        sys.exit(f"Invalid GPU usage batch: {error}")
    except sqlite3.Error:
        sys.exit("GPU usage storage is unavailable")
