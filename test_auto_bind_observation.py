"""A restarted instance with a learned recipe is rebound automatically."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import monitor_dashboard


class AutoBindObservationTests(unittest.TestCase):
    def _recipe(self, workspace):
        return {
            'observation_source': {
                'log_path': '.hook/events.jsonl',
                'fields': {'event': 'event', 'pid': 'pid', 'timestamp': 'timestamp'},
                'event_names': {'user.input': 'user.input'},
            },
            'hook': {'workspace': str(workspace)},
        }

    def test_binds_when_exact_match_and_log_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            run.mkdir()
            workspace = Path(tmp) / 'workspace'
            (workspace / '.hook').mkdir(parents=True)
            (workspace / '.hook' / 'events.jsonl').write_text('{"event":"user.input"}\n')
            old = os.environ.get('ASG_RUN_DIR')
            os.environ['ASG_RUN_DIR'] = str(run)
            try:
                agent = {'instance_id': '4242:1000.5',
                         'adapter': {'match_status': 'exact', 'historical_recipe': self._recipe(workspace)}}
                monitor_dashboard._auto_bind_observations([agent])
                data = json.loads((run / 'observations.json').read_text())
                self.assertIn('4242:1000.5', data)
                self.assertEqual(data['4242:1000.5']['target'], {'pid': 4242, 'create_time': 1000.5})
            finally:
                if old is None:
                    os.environ.pop('ASG_RUN_DIR', None)
                else:
                    os.environ['ASG_RUN_DIR'] = old

    def test_skips_when_log_absent_or_not_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            run.mkdir()
            workspace = Path(tmp) / 'workspace'
            (workspace / '.hook').mkdir(parents=True)  # log file intentionally absent
            old = os.environ.get('ASG_RUN_DIR')
            os.environ['ASG_RUN_DIR'] = str(run)
            try:
                exact = {'instance_id': '1:2', 'adapter': {'match_status': 'exact',
                         'historical_recipe': self._recipe(workspace)}}
                similar = {'instance_id': '3:4', 'adapter': {'match_status': 'similar',
                           'historical_recipe': self._recipe(workspace)}}
                monitor_dashboard._auto_bind_observations([exact, similar])
                self.assertFalse((run / 'observations.json').exists())
            finally:
                if old is None:
                    os.environ.pop('ASG_RUN_DIR', None)
                else:
                    os.environ['ASG_RUN_DIR'] = old


if __name__ == '__main__':
    unittest.main()
