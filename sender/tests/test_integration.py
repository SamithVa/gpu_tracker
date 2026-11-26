"""Exercise the sender wire format against the actual hub receiver."""
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import install
import sender
import uploader

ROOT = sender.ROOT.parent
spec = importlib.util.spec_from_file_location('hub_receiver', ROOT / 'hub' / 'ingest.py')
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class IntegrationTests(unittest.TestCase):
    def test_real_receiver_accepts_sender_batch_and_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / 'checkout'
            (checkout / 'hub').mkdir(parents=True)
            (checkout / 'state').mkdir()
            command = checkout / 'gpuview_ingest.py'
            shutil.copyfile(ROOT / command.name, command)
            shutil.copyfile(ROOT / 'hub' / 'ingest.py', checkout / 'hub' / 'ingest.py')
            local = sender.connect(Path(directory))
            hub = sqlite3.connect(checkout / 'state' / 'usage.sqlite3')
            self.addCleanup(local.close)
            self.addCleanup(hub.close)
            hub.executescript(receiver.SCHEMA)
            now = int(time.time())
            users = [dict(username='example-user', gpus=2, memory_mib=40960)]
            sender.save_interval(local, (now-60, 0, users), (now-30, 30, []))
            sender.save_interval(local, (now-30, 30, []), (now, 60, []))
            delivered = []

            def receive(payload, **kwargs):
                delivered.append(payload)
                result = subprocess.run(
                    [sys.executable, '-I', str(command), 'test-sender'],
                    input=payload, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '')

            with patch.object(uploader, 'send', side_effect=receive):
                self.assertEqual(uploader.upload(local, verify=True), 2)
            self.assertEqual(delivered[0], delivered[1])
            self.assertEqual(hub.execute('SELECT COUNT(*) FROM remote_intervals').fetchone()[0], 2)
            self.assertEqual(hub.execute('SELECT username,gpus,memory_mib FROM remote_usage').fetchone(),
                             ('example-user', 2, 40960))

    def test_units_use_selected_paths(self):
        rendered = install.units('/tmp/custom state', sys.executable)
        self.assertIn('--state-dir "/tmp/custom state" collect', rendered['gpuview-sender.service'])
        self.assertIn('--state-dir "/tmp/custom state" upload', rendered['gpuview-upload.service'])
        self.assertIn('OnUnitInactiveSec=3min', rendered['gpuview-upload.timer'])
        self.assertEqual(install.quote('a%b$c'), '"a%%b$$c"')

    def test_uploader_status_uses_explicit_state(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(sender.ROOT / 'uploader.py'),
                                     '--state-dir', directory, 'status'],
                                    check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)['unsent_intervals'], 0)
            self.assertTrue((Path(directory) / 'upload.log').exists())
