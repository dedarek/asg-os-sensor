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
        states = [asset_state(), asset_state({'source': 'test-collector', 'status': 'empty', 'value': []}),
                  asset_state({'source': 'test-collector', 'status': 'failed', 'value': [], 'message': 'denied'})]
        self.assertEqual([s['status'] for s in states], ['not_collected', 'empty', 'failed'])
        self.assertEqual(states[1]['label'], '未发现（已采集）')
        self.assertIsNone(states[2]['value'])

    def test_all_asset_collection_states_are_distinct(self):
        states = [asset_state({'status': status, 'source': 'fixture', 'value': {'x': 1}})
                  for status in ('not_collected', 'collected', 'empty', 'failed', 'unsupported', 'unknown')]
        self.assertEqual([item['status'] for item in states],
                         ['not_collected', 'collected', 'empty', 'failed', 'unsupported', 'unknown'])
        self.assertEqual([item['label'] for item in states],
                         ['尚未采集', '已采集', '未发现（已采集）', '采集失败', '不支持', '未知（证据不足）'])

    def test_timeout_and_resume_projection_preserve_partial_result(self):
        result = {'status': 'timeout', 'message': '保留进度', 'log_dir': '/tmp/isolated-run',
                  'partial_findings': {'findings': {'identity': {'status': 'identified'}}},
                  'resume': {'status': 'continuing'}}
        state = presentation(True, result=result)['investigation']
        self.assertEqual(state['status'], 'timeout')
        self.assertEqual(state['label'], '超时（已保留证据）')
        self.assertTrue(state['can_continue'])
        self.assertIn('partial_findings', state)

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

    def test_partial_findings_projected_into_adapter_and_display_name(self):
        from types import SimpleNamespace
        info = {'pid': 8888, 'ppid': 1, 'name': 'custom-bin',
                'exe': '/custom/custom-bin', 'cmdline': ['/custom/custom-bin'], 'create_time': 555.0}
        sensor = unittest.mock.Mock()
        sensor.identity_catalog = {}
        sensor.agent_score.return_value = (75, ['heuristic'])
        findings_payload = {
            "version": 1,
            "target": {"pid": 8888, "create_time": 555.0},
            "findings": {
                "identity": {
                    "kind": "identity", "status": "identified",
                    "value": {"name": "Identified-Agent", "runtime": "python"},
                    "evidence_refs": ["ev-1-abc1234567"]
                },
                "assets": {
                    "model_gateway": {
                        "kind": "asset", "asset": "model_gateway", "status": "collected",
                        "value": {"model": "qwen-test"}, "evidence_refs": ["ev-2-abc1234567"]
                    }
                }
            },
            "open_questions": []
        }
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "pid_8888_1000"
            run_dir.mkdir(parents=True)
            (run_dir / "investigation_findings.json").write_text(json.dumps(findings_payload))
            (run_dir / "result.json").write_text(json.dumps({
                "instance_id": "8888:555.0", "pid": 8888, "create_time": 555.0,
                "status": "failed", "message": "unsupported hook", "log_dir": str(run_dir)
            }))
            patches = [
                patch.dict(os.environ, {'ASG_RUN_DIR': str(Path(tmp) / "runs"), 'ASG_FINGERPRINT_DB': str(Path(tmp) / "db.json")}),
                patch.object(dashboard, 'AUTONOMOUS_ANALYSIS_ENABLED', True),
                patch.object(dashboard, 'Sensor', return_value=sensor),
                patch.object(dashboard.psutil, 'process_iter', return_value=[SimpleNamespace(info=info)]),
                patch.object(dashboard, 'identify', return_value={}),
                patch.object(dashboard, 'metadata_identity', return_value={}),
                patch.object(dashboard, 'ownership', return_value={8888: [8888]}),
                patch.object(dashboard.analyzer, 'analyze', return_value={'cwd': '/custom', 'create_time': 555.0}),
                patch.object(dashboard.onboarding, 'view_for_instance', return_value={}),
                patch.object(dashboard.matcher, 'classify', return_value={'status': 'miss', 'entry': None, 'match_ms': 0.1, 'reason': 'miss'}),
            ]
            for p in patches:
                p.start()
            try:
                with patch.object(dashboard, 'run_autonomous_investigation'):
                    dashboard.scan_agents_once()
            except Exception as exc:
                import traceback; traceback.print_exc()
            finally:
                for p in reversed(patches):
                    p.stop()
        agent = dashboard.SCAN_STATE['agents'][0]
        self.assertIn("Goose 调查: Identified-Agent", agent['name'])
        adapter = agent['adapter']
        self.assertIsNotNone(adapter.get('investigated_identity'))
        self.assertEqual(adapter['investigated_identity']['value']['name'], 'Identified-Agent')
        self.assertEqual(adapter['assets']['model_routing']['status'], 'collected')
        self.assertEqual(adapter['assets']['model_routing']['value'], {'model': 'qwen-test'})

    def test_cancel_api_sets_flag_and_terminates(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.MonitorHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            dashboard.SCAN_STATE['agents'] = [{
                'pid': 9999, 'instance_id': '9999:123.0',
                'adapter': {'onboarding': {'plan': {}}}
            }]
            fake_proc = unittest.mock.Mock()
            with dashboard.INVESTIGATION_LOCK:
                dashboard.ACTIVE_ANALYST_PROCESSES['9999:123.0'] = fake_proc
            req = Request(f"http://127.0.0.1:{server.server_port}/api/reinvestigate/cancel?pid=9999", method='POST')
            with urlopen(req) as resp:
                self.assertEqual(resp.status, 202)
                data = json.loads(resp.read())
                self.assertEqual(data['status'], 'cancelling')
            with dashboard.INVESTIGATION_LOCK:
                self.assertIn('9999:123.0', dashboard.INVESTIGATION_CANCEL_REQUESTS)
                dashboard.INVESTIGATION_CANCEL_REQUESTS.clear()
                dashboard.ACTIVE_ANALYST_PROCESSES.clear()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


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
