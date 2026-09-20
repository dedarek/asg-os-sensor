"""G07: no SOC package -> investigation is requested exactly once per instance.

The durable lifecycle is: catalog miss marks needs_investigation, one
reinvestigate request is armed, repeated scans must not re-arm an identical
investigation, and after the investigation produces a package the next scan
installs it without any further investigation request.
"""
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from .endpoint import Endpoint
from .discovery import Discovery


class _Response:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class InvestigationLifecycleTest(unittest.TestCase):
    def target(self):
        return {'name': 'Unknown', 'asg_instance_id': '42:123.5', 'platform': 'mystery',
                'workspace': '/', 'collection_environment': {}}

    def run_refresh(self, endpoint, discovery, state_payload, reinvestigations):
        def open_impl(url, *args, **kwargs):
            full = url if isinstance(url, str) else url.full_url
            if '/api/reinvestigate' in full:
                reinvestigations.append(full)
                return _Response({'status': 'started'})
            return _Response(state_payload)

        with patch('integrations.soc_inventory.discovery.build_opener') as opener, \
             patch('integrations.soc_inventory.discovery.confirmed', side_effect=lambda state: [self.target()]), \
             patch('psutil.Process') as process:
            opener.return_value.open.side_effect = open_impl
            process.return_value.create_time.return_value = 123.5
            return discovery.refresh()

    def test_investigation_requested_once_until_package_exists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            cfg = {'backend_url': 'http://127.0.0.1:1', 'state_dir': directory, 'agents': [],
                   'discovery': {'application_key_file': 'unused'}, 'soc_installation': True}
            endpoint = Endpoint(cfg)
            endpoint.request = lambda *args: {'agent_id': 'asg-test', 'api_key': 'k'}
            discovery = Discovery(endpoint)
            state = {'agents': []}
            reinvestigations = []
            calls = {'install': 0}

            def fake_install(ep, agent, exe, execute=False, upgrade=False, selected=None):
                calls['install'] += 1
                if calls['install'] <= 4:
                    return {'status': 'needs_investigation', 'route': 'protocol_then_goose',
                            'reason': 'no_compatible_SOC_package', 'model_calls': 0}
                return {'status': 'installed', 'artifact_id': 'learned-1', 'checksum': 'sha256:x',
                        'transport': 'soc-direct-v1', 'model_calls': 0}

            with patch('integrations.soc_inventory.soc_onboarding.install', side_effect=fake_install):
                self.run_refresh(endpoint, discovery, state, reinvestigations)
                row = endpoint.db.execute('SELECT result FROM soc_onboarding').fetchone()
                first = json.loads(row[0])
                self.assertEqual(first['status'], 'needs_investigation')
                self.assertTrue(first.get('investigation_requested'))
                self.assertEqual(len(reinvestigations), 1)

                # Repeated scans with the same catalog miss must not re-arm.
                self.run_refresh(endpoint, discovery, state, reinvestigations)
                self.assertEqual(len(reinvestigations), 1)
                row = endpoint.db.execute('SELECT result FROM soc_onboarding').fetchone()
                self.assertEqual(json.loads(row[0])['status'], 'needs_investigation')

                # After the investigation produced a package the next scan installs it.
                self.run_refresh(endpoint, discovery, state, reinvestigations)
                row = endpoint.db.execute('SELECT result FROM soc_onboarding').fetchone()
                done = json.loads(row[0])
                self.assertEqual(done['status'], 'installed')
                self.assertEqual(done['artifact_id'], 'learned-1')
                self.assertEqual(len(reinvestigations), 1)
            endpoint.db.close()


if __name__ == '__main__':
    unittest.main()
