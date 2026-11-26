import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from sender import connect
import uploader


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name))
        uploader.prepare(self.db)
        start = int(time.time()) - 6000
        with self.db:
            self.db.executemany('INSERT INTO intervals VALUES (?,?,?)',
                                [(start + i*30, start + (i+1)*30, '[]') for i in range(150)])

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_bounded_batches_and_order(self):
        payload = uploader.batch(self.db)
        intervals = json.loads(payload)['intervals']
        self.assertEqual(len(intervals), 100)
        self.assertLessEqual(len(payload.encode()), 512*1024)
        self.assertEqual(intervals, sorted(intervals, key=lambda row: row['start_utc']))

    def test_byte_limit_uses_utf8(self):
        with patch.object(uploader, 'MAX_BYTES', 200):
            payload = uploader.batch(self.db)
        self.assertLessEqual(len(payload.encode()), 200)
        self.assertLess(len(json.loads(payload)['intervals']), 100)

    def test_verification_requires_identical_retry_before_cursor(self):
        with patch.object(uploader, 'send', side_effect=[None, RuntimeError('connection lost')]) as send:
            with self.assertRaises(RuntimeError):
                uploader.upload(self.db, verify=True)
            first = send.call_args_list[0].args[0]
            self.assertEqual(first, send.call_args_list[1].args[0])
        self.assertEqual(self.db.execute('SELECT last_start,verified FROM upload_state').fetchone(), (-1, 0))
        self.assertEqual(uploader.batch(self.db), first)
        with patch.object(uploader, 'send') as send:
            self.assertEqual(uploader.upload(self.db, verify=True), 2)
            self.assertEqual([call.args[0] for call in send.call_args_list], [first, first])
        self.assertEqual(self.db.execute('SELECT verified,pending FROM upload_state').fetchone(), (1, None))

    def test_schedule_gated_then_drains_backlog(self):
        with self.assertRaises(RuntimeError):
            uploader.upload(self.db)
        with patch.object(uploader, 'send'):
            uploader.upload(self.db, verify=True)
            self.assertEqual(uploader.upload(self.db), 148)
            self.assertEqual(uploader.upload(self.db), 0)

    def test_output_or_failure_rejects_acknowledgment(self):
        for code, stdout, stderr in [(1, b'', b''), (0, b'ok', b''), (0, b'', b'warning')]:
            with patch.object(uploader.subprocess, 'run', return_value=subprocess.CompletedProcess([],code,stdout,stderr)):
                with self.assertRaises(RuntimeError):
                    uploader.send('{}')

    def test_expired_data_is_retained(self):
        with patch.object(uploader.time, 'time', return_value=time.time()+32*86400):
            with self.assertRaises(RuntimeError):
                uploader.batch(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM intervals').fetchone()[0], 150)


if __name__ == '__main__':
    unittest.main()
