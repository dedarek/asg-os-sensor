import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from runtime.status import presentation, asset_state
from test_matcher_stage1 import _mcp_get_prior
import monitor_dashboard as dashboard


class TruthTests(unittest.TestCase):
    def test_disabled_overrides_stale_running(self):
        state = presentation(False, True, {'status': 'succeeded'})
        self.assertEqual(state['investigation']['status'], 'disabled')
        self.assertFalse(state['investigation']['can_request'])
        self.assertTrue(all(a['status'] == 'not_collected' for a in state['assets'].values()))

    def test_collection_absent_empty_failed(self):
        states = [asset_state(), asset_state({'source': 'test-collector', 'status': 'collected', 'value': []}),
                  asset_state({'source': 'test-collector', 'status': 'failed', 'value': [], 'message': 'denied'})]
        self.assertEqual([s['status'] for s in states], ['not_collected', 'collected', 'failed'])
        self.assertEqual(states[1]['label'], '未发现（已采集）')
        self.assertIsNone(states[2]['value'])

    def test_recipe_does_not_install_hook(self):
        # Caller deliberately supplies a success record: installation is independent.
        state = presentation(True, result={'status': 'succeeded'})
        self.assertEqual(state['hook_state']['status'], 'not_installed')
        self.assertFalse(state['hook_state']['verified'])
        self.assertEqual(state['host_platform'], '未知')
        self.assertIn('桌面', presentation(True, identity={'source': 'bundle-metadata'})['host_platform'])

    def test_related_processes_without_events(self):
        state = {'process_pids': [101, 102], 'adapter': presentation(False)}
        self.assertEqual(len(state['process_pids']), 2)
        self.assertEqual(state['adapter']['assets']['child_executions']['status'], 'not_collected')
        self.assertIn('a.process_pids', dashboard.HTML_PAGE)
        self.assertIn("assetText(a.adapter, 'child_executions')", dashboard.HTML_PAGE)

    def test_disabled_http_and_worker(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.MonitorHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            with patch.object(dashboard, 'AUTONOMOUS_ANALYSIS_ENABLED', False), patch.object(dashboard.analyzer, 'analyze') as analyze:
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(f'http://127.0.0.1:{server.server_port}/api/reinvestigate?pid=123', method='POST'))
                self.assertEqual(error.exception.code, 503)
                self.assertEqual(json.load(error.exception), presentation(False)['investigation'])
                dashboard.run_autonomous_investigation(123, {})
                analyze.assert_not_called()
            self.assertIn("investigation.can_request ? '' : 'disabled'", dashboard.HTML_PAGE)
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def test_corrupt_prior_mcp_error_preserves_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'prior.json'; path.write_text('{broken')
            env = dict(os.environ, ASG_FINGERPRINT_DB=str(path)); env.pop('PYTHONPATH', None)
            result = _mcp_get_prior(env)
            self.assertIn('error', result)
            self.assertIsNone(result['result'])
            self.assertEqual(path.read_text(), '{broken')

    def test_scan_recipe_and_related_processes_stay_independent(self):
        from types import SimpleNamespace
        info = {'pid': 1234567, 'ppid': 1, 'name': 'random-runtime',
                'exe': '/synthetic/runtime', 'cmdline': ['/synthetic/runtime'], 'create_time': 123.0}
        sensor = unittest.mock.Mock()
        sensor.identity_catalog = {}
        sensor.agent_score.return_value = (70, ['synthetic behavior'])
        recipe = {'id': 'history', 'hook_recipe': {'model_routing': {'model': 'old-model'}}}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_FINGERPRINT_DB': str(Path(tmp)/'db.json')}), \
             patch.object(dashboard, 'AUTONOMOUS_ANALYSIS_ENABLED', False), \
             patch.object(dashboard, 'Sensor', return_value=sensor), \
             patch.object(dashboard.psutil, 'process_iter', return_value=[SimpleNamespace(info=info)]), \
             patch.object(dashboard, 'identify', return_value={}), \
             patch.object(dashboard, 'metadata_identity', return_value={}), \
             patch.object(dashboard, 'ownership', return_value={1234567: [1234567, 1234568]}), \
             patch.object(dashboard.analyzer, 'analyze', return_value={}), \
             patch.object(dashboard.matcher, 'match', return_value=(recipe, 0.1)):
            dashboard.scan_agents_once()
        agent = dashboard.SCAN_STATE['agents'][0]
        self.assertEqual(agent['process_pids'], [1234567, 1234568])
        self.assertTrue(agent['adapter']['matched'])
        self.assertEqual(agent['adapter']['hook_state']['status'], 'not_installed')
        self.assertEqual(agent['adapter']['investigation']['status'], 'disabled')
        self.assertIsNone(agent['adapter']['model_routing'])
        self.assertEqual(agent['adapter']['assets']['child_executions']['status'], 'not_collected')

    def test_ui_has_no_false_promises(self):
        for text in ('自动解析中', '自动读取解析中', '已适配挂接', '待流量流入自动激活', '全链路收敛安全', '无活跃子执行'):
            self.assertNotIn(text, dashboard.HTML_PAGE)


if __name__ == '__main__':
    unittest.main()
