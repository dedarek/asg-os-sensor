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
            # The new rule: the log tail must carry the live instance's own pid.
            (workspace / '.hook' / 'events.jsonl').write_text(
                '{"event":"user.input","pid":4242}\n')
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

    def test_stale_log_from_other_pid_is_not_bound(self):
        # Regression: a live instance was once bound to a dead test-profile log
        # that existed on disk but only carried another instance's records.
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            run.mkdir()
            workspace = Path(tmp) / 'workspace'
            (workspace / '.hook').mkdir(parents=True)
            (workspace / '.hook' / 'events.jsonl').write_text(
                '{"event":"user.input","pid":99999}\n')
            old = os.environ.get('ASG_RUN_DIR')
            os.environ['ASG_RUN_DIR'] = str(run)
            try:
                agent = {'instance_id': '4242:1000.5',
                         'adapter': {'match_status': 'exact', 'historical_recipe': self._recipe(workspace)}}
                monitor_dashboard._auto_bind_observations([agent])
                self.assertFalse((run / 'observations.json').exists())
            finally:
                if old is None:
                    os.environ.pop('ASG_RUN_DIR', None)
                else:
                    os.environ['ASG_RUN_DIR'] = old

    def test_receipt_log_outranks_learned_workspace(self):
        # The endpoint receipt names the log this instance provably writes to;
        # it must win over the (possibly stale) learned recipe workspace.
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            run.mkdir()
            workspace = Path(tmp) / 'workspace'
            (workspace / '.hook').mkdir(parents=True)
            (workspace / '.hook' / 'events.jsonl').write_text('{"event":"user.input","pid":111}\n')
            receipt_dir = Path(tmp) / 'endpoint'
            receipt_dir.mkdir()
            import sqlite3
            con = sqlite3.connect(receipt_dir / 'outbox.sqlite')
            con.execute('CREATE TABLE soc_onboarding(instance TEXT PRIMARY KEY, result TEXT)')
            receipt = {'status': 'activation_verified', 'result': {'activation': {
                'status': 'activation_verified', 'pid': 4242, 'create_time': 1000.5,
                'log_path': str(workspace / 'real' / 'events.jsonl')}}}
            con.execute('INSERT INTO soc_onboarding VALUES (?,?)',
                        ('4242:1000.5', json.dumps(receipt)))
            con.commit()
            con.close()
            real = workspace / 'real'
            real.mkdir()
            (real / 'events.jsonl').write_text('{"event":"user.input","pid":4242}\n')
            old = {k: os.environ.get(k) for k in ('ASG_RUN_DIR', 'ASG_ENDPOINT_DB')}
            os.environ['ASG_RUN_DIR'] = str(run)
            os.environ['ASG_ENDPOINT_DB'] = str(receipt_dir / 'outbox.sqlite')
            try:
                agent = {'instance_id': '4242:1000.5',
                         'adapter': {'match_status': 'exact', 'historical_recipe': self._recipe(workspace)}}
                monitor_dashboard._auto_bind_observations([agent])
                data = json.loads((run / 'observations.json').read_text())
                bound = json.loads(Path(data['4242:1000.5']['config_path']).read_text())
                self.assertEqual(bound['log_path'], str(real / 'events.jsonl'))
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

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
