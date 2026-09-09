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
             patch.object(dashboard.matcher, 'classify', return_value={'status': 'exact', 'entry': recipe, 'match_ms': 0.1, 'reason': 'test'}):
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


    def test_pid_reuse_does_not_leak_lifecycle(self):
        # A 实例调查成功并写入结果；PID 复用后的 B 实例（不同 create_time）不得看到 A 的 result/running/冷却。
        with dashboard.INVESTIGATION_LOCK:
            dashboard.INVESTIGATION_RESULTS.clear()
            dashboard.INVESTIGATING_INSTANCES.clear()
            dashboard.INVESTIGATION_RETRY_AT.clear()
        inst_a = "123:111.0"
        dashboard._record_investigation_result(inst_a, 123, 111.0, "succeeded", "saved A")
        with dashboard.INVESTIGATION_LOCK:
            dashboard.INVESTIGATION_RETRY_AT[inst_a] = dashboard.now() + 999999
        inst_b = "123:222.0"
        self.assertNotIn(inst_b, dashboard.INVESTIGATION_RESULTS)
        self.assertNotIn(inst_b, dashboard.INVESTIGATION_RETRY_AT)
        with dashboard.INVESTIGATION_LOCK:
            dashboard.INVESTIGATING_INSTANCES[inst_a] = 111.0
        self.assertNotIn(inst_b, dashboard.INVESTIGATING_INSTANCES)
        state_b = presentation(True, running=(inst_b in dashboard.INVESTIGATING_INSTANCES),
                               result=dashboard.INVESTIGATION_RESULTS.get(inst_b))['investigation']
        self.assertEqual(state_b['status'], 'not_scheduled')
        with dashboard.INVESTIGATION_LOCK:
            dashboard.INVESTIGATION_RESULTS.clear()
            dashboard.INVESTIGATING_INSTANCES.clear()
            dashboard.INVESTIGATION_RETRY_AT.clear()

    def test_record_result_uses_frozen_create_time_not_live_pid(self):
        # 结果使用调用方冻结的 create_time；PID 在调查期间退出/复用不会误归属。
        with dashboard.INVESTIGATION_LOCK:
            dashboard.INVESTIGATION_RESULTS.clear()
        dashboard._record_investigation_result("777:100.0", 777, 100.0, "succeeded", "frozen")
        saved = dashboard.INVESTIGATION_RESULTS.get("777:100.0")
        self.assertIsNotNone(saved)
        self.assertEqual(saved['create_time'], 100.0)
        self.assertEqual(saved["instance_id"], "777:100.0")
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            dashboard._record_investigation_result("777:100.0", 777, 100.0, "succeeded", "frozen", rd)
            data = json.loads((rd / 'result.json').read_text())
            self.assertEqual(data["instance_id"], "777:100.0")
            self.assertEqual(data['create_time'], 100.0)
        with dashboard.INVESTIGATION_LOCK:
            dashboard.INVESTIGATION_RESULTS.clear()


    def test_cross_instance_reuse_no_reinvestigation(self):
        # 合成目标（随机命名脚本）：实例 A 调查落库后，同入口新实例（新 pid+create_time）
        # 必须 exact 复用，且调查函数不再被调用（无新增 Goose）。
        from runtime import matcher as m, compatibility as compat
        from runtime.recipe_validation import validate as v
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ('agent-' + 'xyz987')  # 随机命名、非产品名单
            script.write_text('fastapi_worker = True')
            calls = {'count': 0}
            def fake_investigate(pid, struct, force=False):
                calls['count'] += 1
                return None
            with patch.dict(os.environ, {'ASG_FINGERPRINT_DB': str(root / 'fp.json'),
                                         'ASG_AUDIT_DIR': str(root / 'audit'),
                                         'ASG_RECIPE_DIR': str(root / 'recipes')}):
                c1 = compat.observe(str(script), [str(script)], str(root))
                struct_a = {'exe': script.name, 'runtime': 'native', 'create_time': 100.0, 'compatibility': c1}
                recipe_a = {'agent_identity_name': script.name, 'match_features': {'runtime': 'native'},
                            'hook': {'method': 'unsupported', 'restart_required': 'unknown', 'capabilities': [],
                                     'verification': 'pending', 'rollback': 'none', 'limitations': ['unknown']},
                            'observation': 'obs', 'fallback': 'manual', 'evidence_refs': ['ev-1-abcdef1234'], 'confidence': 0.9}
                ev = {'tool': 'get_target_context', 'error': None,
                      'result': {'target': {'pid': 4242, 'create_time': 100.0}},
                      'target': {'pid': 4242, 'create_time': 100.0}}
                (root / 'ev-1-abcdef1234.json').write_text(json.dumps(ev))
                out = v(recipe_a, root, target={'pid': 4242, 'create_time': 100.0})
                m.remember_verified(struct_a, recipe_a, out['evidence'])
                # 模拟第二次启动：新 pid+create_time，同入口
                c2 = compat.observe(str(script), [str(script)], str(root))
                struct_b = {'exe': script.name, 'runtime': 'native', 'create_time': 200.0, 'compatibility': c2}
                with patch.object(dashboard, 'run_autonomous_investigation', side_effect=fake_investigate) as ri:
                    classified = m.classify(struct_b)
                    self.assertEqual(classified['status'], 'exact')
                    # 循环一次扫描模拟（inst B 已 exact，不应拉起调查）
                    retry_ready = dashboard.now() >= dashboard.INVESTIGATION_RETRY_AT.get("4242:200.0", 0)
                    is_matched = classified['status'] == 'exact'
                    is_investigating = "4242:200.0" in dashboard.INVESTIGATING_INSTANCES
                    if retry_ready and not is_matched and not is_investigating:
                        ri(struct_b['exe'], struct_b)
                    self.assertEqual(calls['count'], 0)  # 没有新增 Goose 调用


if __name__ == '__main__':
    unittest.main()
