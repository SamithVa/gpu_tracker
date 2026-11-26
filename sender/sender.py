#!/usr/bin/env python3
"""Local GPU occupancy collector. History contains only aggregate usage."""

import argparse
import csv
import fcntl
import io
import json
import logging
import os
from pathlib import Path
import pwd
import sqlite3
import subprocess
import time

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = Path(os.environ.get('GPUVIEW_STATE_DIR', ROOT / 'state'))


def owner(pid):
    try:
        return pwd.getpwuid(Path('/proc', str(pid)).stat().st_uid).pw_name
    except (OSError, KeyError):
        return '?'


def aggregate(output, resolve=owner):
    users = {}
    seen = set()
    for row in csv.reader(io.StringIO(output)):
        if not row or not any(value.strip() for value in row):
            continue
        if len(row) != 3:
            raise ValueError('unexpected compute-apps output')
        uuid, pid, memory = (value.strip() for value in row)
        pid, memory = int(pid), int(memory)
        if not uuid.startswith('GPU-') or pid <= 0 or memory < 0:
            raise ValueError('invalid compute-apps values')
        # A repeated row must not count process memory twice.
        if (uuid, pid) in seen:
            raise ValueError('duplicate compute-apps row')
        seen.add((uuid, pid))
        entry = users.setdefault(resolve(pid), {'uuids': set(), 'memory_mib': 0})
        entry['uuids'].add(uuid)
        entry['memory_mib'] += memory
    return [dict(username=name, gpus=len(entry['uuids']), memory_mib=entry['memory_mib'])
            for name, entry in sorted(users.items())]


def query():
    result = subprocess.run(
        ['/usr/bin/nvidia-smi', '--query-compute-apps=gpu_uuid,pid,used_gpu_memory',
         '--format=csv,noheader,nounits'],
        check=True, capture_output=True, text=True, timeout=10,
        env={**os.environ, 'LC_ALL': 'C'})
    return aggregate(result.stdout)


def connect(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_uid != os.getuid():
        raise RuntimeError('state directory belongs to another account')
    state.chmod(0o700)
    db = sqlite3.connect(state / 'usage.sqlite3', timeout=15)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('''CREATE TABLE IF NOT EXISTS intervals (
        start_utc INTEGER PRIMARY KEY,
        end_utc INTEGER NOT NULL CHECK(end_utc > start_utc AND end_utc-start_utc <= 45),
        users_json TEXT NOT NULL)''')
    db.commit()
    return db


def save_interval(db, previous, current):
    if previous is None:
        return False
    start, monotonic_start, users = previous
    end, monotonic_end, _ = current
    elapsed = monotonic_end - monotonic_start
    if not (0 < end - start <= 45 and 0 < elapsed <= 45):
        return False
    # Reject wall-clock changes rather than attribute usage across them.
    if abs((end - start) - elapsed) > 2:
        return False
    latest = db.execute('SELECT MAX(end_utc) FROM intervals').fetchone()[0]
    if latest is not None and start < latest:
        return False
    with db:
        db.execute('INSERT INTO intervals VALUES (?, ?, ?)',
                   (start, end, json.dumps(users, separators=(',', ':'))))
    return True


def collect(state):
    db = connect(state)
    with (state / 'collector.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = None
        while True:
            tick = time.monotonic()
            try:
                timestamp = int(time.time())
                users = query()
                current = (timestamp, tick, users)
                saved = save_interval(db, previous, current)
                previous = current
                logging.info('sample succeeded: users=%d interval_saved=%s', len(users), saved)
            except (subprocess.SubprocessError, OSError, ValueError):
                # Never bridge a failed sample or save it as an idle interval.
                previous = None
                logging.error('sample failed; usage across this gap will not be recorded')
            time.sleep(max(0, 30 - (time.monotonic() - tick)))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('command', choices=['collect', 'probe', 'status'])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'collect':
        collect(args.state_dir)
    elif args.command == 'probe':
        print(json.dumps({'users': query()}, indent=2))
    else:
        with connect(args.state_dir) as db:
            count, start, end = db.execute(
                'SELECT COUNT(*), MIN(start_utc), MAX(end_utc) FROM intervals').fetchone()
        print(json.dumps(dict(intervals=count, first_start_utc=start, last_end_utc=end)))


if __name__ == '__main__':
    main()
