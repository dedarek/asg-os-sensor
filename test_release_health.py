import json
import tempfile
import time
import unittest
from pathlib import Path

from release.service_main import endpoint_probe


class ReleaseHealthTests(unittest.TestCase):
    def test_endpoint_probe_rejects_previous_process_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = home / 'state' / 'endpoint'
            state.mkdir(parents=True)
            (state / 'loop-beat.json').write_text(json.dumps({'pid': 10, 't': time.time()}))
            (state / 'bridge-beat.json').write_text(json.dumps({'pid': 10, 't': time.time()}))
            (state / 'soc-health.json').write_text(json.dumps({
                'pid': 10, 'checked_at': time.time(), 'state': 'ok', 'detail': 'old'}))

            stale = endpoint_probe(home, endpoint_pid=11)
            self.assertIsNone(stale['loop_age_s'])
            self.assertIsNone(stale['bridge_age_s'])
            self.assertEqual(stale['soc']['state'], 'unknown')

            current = endpoint_probe(home, endpoint_pid=10)
            self.assertIsNotNone(current['loop_age_s'])
            self.assertEqual(current['soc']['state'], 'ok')


if __name__ == '__main__':
    unittest.main()
