#!/usr/bin/env python3
"""Install user systemd units for this checkout; never enroll keys on the hub."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from sender import DEFAULT_STATE, ROOT, connect


def quote(value):
    # systemd parses specifiers even inside quotes.
    return json.dumps(str(value).replace('%', '%%').replace('$', '$$'), ensure_ascii=False)


def units(state, python):
    state, python = Path(state).resolve(), Path(python).resolve()
    prefix = '[Unit]\nDescription=GPUView {description}\n\n[Service]\n'
    common = 'UMask=0077\nNoNewPrivileges=yes\n'
    collector = prefix.format(description='local GPU occupancy collector') + (
        'Type=simple\nExecStart={} {} --state-dir {} collect\n'
        'Restart=on-failure\nRestartSec=10\n').format(
            quote(python), quote(ROOT / 'sender.py'), quote(state))
    collector += common + '\n[Install]\nWantedBy=default.target\n'
    uploader = prefix.format(description='history upload') + (
        'Type=oneshot\nExecStart={} {} --state-dir {} upload\nTimeoutStartSec=12min\n').format(
            quote(python), quote(ROOT / 'uploader.py'), quote(state))
    uploader += common
    timer = ('[Unit]\nDescription=Upload GPUView history every three minutes\n\n'
             '[Timer]\nOnStartupSec=30\nOnUnitInactiveSec=3min\n'
             'Unit=gpuview-upload.service\n\n[Install]\nWantedBy=timers.target\n')
    return {'gpuview-sender.service': collector, 'gpuview-upload.service': uploader,
            'gpuview-upload.timer': timer}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('command', choices=['collect', 'uploads'])
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error('Run as the collector account, without sudo')
    state = args.state_dir.expanduser().resolve()
    python = Path(sys.executable).resolve()
    if args.command == 'collect':
        subprocess.run([str(python), str(ROOT / 'sender.py'), 'probe'], check=True)
    else:
        if not (state / 'ssh_config').is_file():
            parser.error('Configure STATE_DIR/ssh_config and enroll the key first')
        with connect(state) as db:
            from uploader import prepare
            prepare(db)
            verified = db.execute('SELECT verified FROM upload_state').fetchone()[0]
        # Reinstallation need not wait for fresh unsent intervals.
        if not verified:
            subprocess.run([str(python), str(ROOT / 'uploader.py'), '--state-dir', str(state), 'verify'], check=True)
        subprocess.run([str(python), str(ROOT / 'uploader.py'), '--state-dir', str(state), 'upload'], check=True)
    config = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd/user'
    config.mkdir(parents=True, exist_ok=True)
    rendered = units(state, python)
    names = ['gpuview-sender.service'] if args.command == 'collect' else ['gpuview-upload.service', 'gpuview-upload.timer']
    for name in names:
        path = config / name
        if path.is_symlink():
            parser.error('Existing unit is a symlink; migrate it explicitly before installing: ' + str(path))
    for name in names:
        (config / name).write_text(rendered[name])
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    target = names[-1]
    subprocess.run(['systemctl', '--user', 'enable', '--now', target], check=True)
    if args.command == 'collect':
        subprocess.run(['systemctl', '--user', 'restart', target], check=True)
    print('Enabled ' + target + '; state: ' + str(state))


if __name__ == '__main__':
    main()
