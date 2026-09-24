"""Crazytest B17: a platform-retired identity must stop local reporting.

SOC treats 'retired' as final (a heartbeat may refresh the timestamp but the
card never returns online).  When either live channel (heartbeat response or
runtime acceptance) reports that verdict, the endpoint must drop the local
enrollment so heartbeat, runtime bridge, and queued flushes all stop speaking
for a closed card.  Re-registration is discovery's decision from fresh
evidence, never an automatic revival.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from .endpoint import Endpoint
from .runtime_bridge import RuntimeBridge


class RetiredIdentityTest(unittest.TestCase):
    def _endpoint(self, root, agent):
        key = Path(root)/'k.key'; key.write_text('k')
        # Real deployments enroll discovered Agents only through the durable
        # 'enrolled' table (static config agents is empty), matching the
        # superseded-vite scenario this test guards.
        agent = dict(agent, key_file=str(Path(root)/(agent['agent_id']+'.key')))
        Path(agent['key_file']).write_text('k')
        e = Endpoint({'state_dir': root, 'backend_url': 'http://127.0.0.1:1',
                      'collection_mode': 'manual', 'agents': [],
                      'discovery': {'application_key_file': 'unused'}})
        e.db.execute('CREATE TABLE IF NOT EXISTS enrolled(instance TEXT PRIMARY KEY,configuration TEXT NOT NULL)')
        e.db.execute('INSERT INTO enrolled VALUES(?,?)',
                     ('1:9', json.dumps({**agent, 'asg_instance_id': '1:9'})))
        e.reload_agents()
        return e

    def test_heartbeat_retired_verdict_drops_local_enrollment(self):
        with tempfile.TemporaryDirectory() as root:
            agent = {'agent_id': 'a1', 'platform': 'example', 'name': 'n', 'workspace': root}
            e = self._endpoint(root, agent)
            e.request = Mock(return_value={'code': 200, 'data': {'status': 'retired'}})
            e.flush = Mock(); e.enqueue = Mock()
            class D:
                def __init__(self, endpoint): self.refresh = Mock(return_value=['a1'])
            import integrations.soc_inventory.discovery as dm
            with patch.object(dm, 'Discovery', D), \
                 patch('integrations.soc_inventory.endpoint.time.sleep', side_effect=InterruptedError):
                with self.assertRaises(InterruptedError): e.run()
            self.assertNotIn('a1', e.agents)
            self.assertEqual(e.db.execute('SELECT count(*) FROM enrolled').fetchone()[0], 0)

    def test_runtime_retired_verdict_drops_local_enrollment(self):
        with tempfile.TemporaryDirectory() as root:
            db_file = Path(root)/'s.db'
            import sqlite3
            db = sqlite3.connect(str(db_file))
            retired = []
            calls = []
            def request(agent, path, data, method='POST'):
                calls.append(path)
                if path == '/api/asg/runtime':
                    return {'accepted': True, 'agent_status': 'retired'}
                return {'commands': [], 'accepted': True}
            endpoint = Mock(db=db, config={'state_dir': root}, request=request)
            endpoint.retire_local = Mock(side_effect=lambda aid: retired.append(aid))
            bridge = RuntimeBridge.__new__(RuntimeBridge)
            bridge.endpoint = endpoint
            bridge.base = 'http://127.0.0.1:8081'
            bridge.events = Mock()
            agent = {'agent_id': 'a1', 'asg_instance_id': '1:9', 'name': 'n'}
            state = {'agents': [{'instance_id': '1:9', 'pid': 1}], 'scan_interval': None}
            bridge.local = lambda path, body=None: {'records': [], 'coverage': {}}
            bridge._runtime_cycle(agent, state, {}, {})
            self.assertEqual(retired, ['a1'])
            # commands must not be polled for an identity that was just retired
            self.assertNotIn('/api/asg/commands', calls)

    def test_retire_local_clears_key_and_instance_receipts(self):
        with tempfile.TemporaryDirectory() as root:
            agent = {'agent_id': 'a1', 'platform': 'example', 'name': 'n', 'workspace': root}
            e = self._endpoint(root, agent)
            e.db.execute('CREATE TABLE IF NOT EXISTS soc_onboarding(instance TEXT PRIMARY KEY,result TEXT)')
            e.db.execute('INSERT INTO soc_onboarding VALUES(?,?)', ('1:9', '{}'))
            key = Path(root)/'a1.key'
            e.retire_local('a1')
            self.assertEqual(e.db.execute('SELECT count(*) FROM enrolled').fetchone()[0], 0)
            self.assertEqual(e.db.execute('SELECT count(*) FROM soc_onboarding').fetchone()[0], 0)
            self.assertFalse(key.exists())
            self.assertNotIn('a1', e.agents)

    def test_online_verdict_keeps_enrollment(self):
        with tempfile.TemporaryDirectory() as root:
            agent = {'agent_id': 'a1', 'platform': 'example', 'name': 'n', 'workspace': root}
            e = self._endpoint(root, agent)
            e.request = Mock(return_value={'code': 200, 'data': {'status': 'online'}})
            e.flush = Mock(); e.enqueue = Mock()
            class D:
                def __init__(self, endpoint): self.refresh = Mock(return_value=['a1'])
            import integrations.soc_inventory.discovery as dm
            with patch.object(dm, 'Discovery', D), \
                 patch('integrations.soc_inventory.endpoint.time.sleep', side_effect=InterruptedError):
                with self.assertRaises(InterruptedError): e.run()
            self.assertIn('a1', e.agents)
            self.assertEqual(e.db.execute('SELECT count(*) FROM enrolled').fetchone()[0], 1)
