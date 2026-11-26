#!/usr/bin/env python3
"""Deliver durable, bounded batches through the hub's forced SSH receiver."""
import argparse
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import time

from sender import DEFAULT_STATE, connect

MAX_BYTES = 512 * 1024


def prepare(db):
    db.execute('''CREATE TABLE IF NOT EXISTS upload_state (
        id INTEGER PRIMARY KEY CHECK(id=1), last_start INTEGER NOT NULL,
        verified INTEGER NOT NULL, pending TEXT)''')
    db.execute('INSERT OR IGNORE INTO upload_state VALUES (1,-1,0,NULL)')
    db.commit()


def batch(db, limit=100):
    last, _, pending = db.execute('SELECT last_start,verified,pending FROM upload_state').fetchone()
    if pending is not None:
        return pending
    rows = db.execute('SELECT start_utc,end_utc,users_json FROM intervals '
                      'WHERE start_utc>? ORDER BY start_utc LIMIT ?', (last, limit)).fetchall()
    intervals = []
    payload = None
    for start, end, users in rows:
        if start < time.time() - 31 * 86400 or end > time.time() + 60:
            raise RuntimeError('Unsent interval outside receiver timestamp window; retained locally')
        entry = dict(start_utc=start, end_utc=end, users=json.loads(users))
        candidate = json.dumps(dict(version=1, intervals=intervals + [entry]),
                               ensure_ascii=False, separators=(',', ':'))
        if len(candidate.encode('utf-8')) > MAX_BYTES:
            if not intervals:
                raise RuntimeError('Single interval exceeds receiver byte limit')
            break
        intervals.append(entry)
        payload = candidate
    if payload is not None:
        with db:
            db.execute('UPDATE upload_state SET pending=? WHERE id=1', (payload,))
    return payload


def send(payload, ssh_config=None):
    ssh_config = ssh_config or DEFAULT_STATE / 'ssh_config'
    result = subprocess.run(
        ['/usr/bin/ssh', '-T', '-F', str(ssh_config), 'gpuview-hub'],
        input=payload.encode('utf-8'), capture_output=True, timeout=60)
    if result.returncode != 0 or result.stdout or result.stderr:
        # Receiver output might include supplied data; keep logs to status and sizes.
        raise RuntimeError('SSH receiver failed: exit=%d stdout_bytes=%d stderr_bytes=%d; batch retained'
                           % (result.returncode, len(result.stdout), len(result.stderr)))


def upload(db, verify=False, ssh_config=None):
    prepare(db)
    verified = db.execute('SELECT verified FROM upload_state').fetchone()[0]
    if not verify and not verified:
        raise RuntimeError('Run verify successfully before scheduled uploads')
    total = 0
    for _ in range(1 if verify else 10):
        payload = batch(db, limit=2 if verify else 100)
        if payload is None:
            if verify:
                raise RuntimeError('No unsent real intervals available for verification')
            break
        send(payload, ssh_config=ssh_config)
        if verify:
            send(payload, ssh_config=ssh_config)  # Identical bytes, including after an interrupted previous attempt.
        intervals = json.loads(payload)['intervals']
        with db:
            db.execute('UPDATE upload_state SET last_start=?,verified=1,pending=NULL WHERE id=1',
                       (intervals[-1]['start_utc'],))
        total += len(intervals)
    logging.info('%s committed: intervals=%d', 'Duplicate verification' if verify else 'Upload', total)
    return total


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--ssh-config', type=Path, help='Default: STATE_DIR/ssh_config')
    parser.add_argument('command', choices=['verify', 'upload', 'status'])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    state = args.state_dir.expanduser().resolve()
    db = connect(state)
    logfile = RotatingFileHandler(state / 'upload.log', maxBytes=1024*1024, backupCount=3)
    logfile.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger().addHandler(logfile)
    try:
        with (state / 'uploader.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prepare(db)
            if args.command == 'status':
                last, verified, pending = db.execute('SELECT last_start,verified,pending FROM upload_state').fetchone()
                unsent = db.execute('SELECT COUNT(*) FROM intervals WHERE start_utc>?', (last,)).fetchone()[0]
                print(json.dumps(dict(last_uploaded_start_utc=last, deduplication_verified=bool(verified),
                                      pending_batch=bool(pending), unsent_intervals=unsent)))
            else:
                upload(db, verify=args.command == 'verify',
                       ssh_config=args.ssh_config or state / 'ssh_config')
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        logging.error('%s', error)
        raise SystemExit(1)
    finally:
        db.close()


if __name__ == '__main__':
    main()
