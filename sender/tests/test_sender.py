import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import sender


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.db = sender.connect(self.state)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_shared_gpu_and_multiple_processes(self):
        data = 'GPU-a, 1, 100\nGPU-a, 2, 200\nGPU-b, 3, 400\nGPU-a, 4, 50\nGPU-b, 5, 10\n'
        users = sender.aggregate(data, lambda pid: {1: 'alice', 2: 'alice', 3: 'alice', 4: 'bob'}.get(pid, '?'))
        self.assertEqual(users, [dict(username='?', gpus=1, memory_mib=10),
                                 dict(username='alice', gpus=2, memory_mib=700),
                                 dict(username='bob', gpus=1, memory_mib=50)])

    def test_idle_intervals_and_previous_snapshot(self):
        users = [dict(username='alice', gpus=1, memory_mib=100)]
        self.assertTrue(sender.save_interval(self.db, (100, 0, users), (130, 30, [])))
        self.assertTrue(sender.save_interval(self.db, (130, 30, []), (160, 60, users)))
        rows = self.db.execute('SELECT * FROM intervals ORDER BY start_utc').fetchall()
        self.assertEqual(json.loads(rows[0][2]), users)
        self.assertEqual(json.loads(rows[1][2]), [])

    def test_gaps_clock_changes_and_overlaps(self):
        for current in [(146, 46, []), (99, 30, []), (130, 100, []), (130, 10, [])]:
            self.assertFalse(sender.save_interval(self.db, (100, 0, []), current))
        self.assertTrue(sender.save_interval(self.db, (100, 0, []), (145, 45, [])))
        self.assertFalse(sender.save_interval(self.db, (120, 20, []), (150, 50, [])))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM intervals').fetchone()[0], 1)

    def test_bad_data_is_not_idle(self):
        for output in ['GPU-a, 1, [N/A]', 'bad output', 'GPU-a, 1, -1',
                       'GPU-a, 1, 10\nGPU-a, 1, 10']:
            with self.assertRaises(ValueError):
                sender.aggregate(output)
        self.assertEqual(sender.aggregate(''), [])

    def test_failure_breaks_interval_chain(self):
        with patch.object(sender, 'connect', return_value=self.db), \
             patch.object(sender.logging, 'info'), patch.object(sender.logging, 'error'), \
             patch.object(sender, 'query', side_effect=[[], subprocess.CalledProcessError(1, 'nvidia-smi'), [], []]), \
             patch.object(sender.time, 'time', side_effect=[100, 130, 160, 190]), \
             patch.object(sender.time, 'monotonic', side_effect=[0, 1, 30, 31, 60, 61, 90, 91]), \
             patch.object(sender.time, 'sleep', side_effect=[None, None, None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                sender.collect(self.state)
        self.assertEqual(self.db.execute('SELECT start_utc,end_utc FROM intervals').fetchall(), [(160, 190)])


if __name__ == '__main__':
    unittest.main()
