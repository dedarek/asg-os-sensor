import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from concurrent.futures import ThreadPoolExecutor
import psutil
from runtime import matcher
from runtime.compatibility import observe
from runtime.collection import collect
from runtime.recipe_validation import validate
from runtime.llm_config import mask_key
from runtime.llm_config import analyst_route, goose_env, tls_exception_enabled


class GooseStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, ASG_FINGERPRINT_DB=str(self.root / 'fp.json'))
        self.env.start()
        self.exe = self.root / 'anonymous'; self.exe.write_bytes(b'native build one')
        self.struct = {'exe': 'anonymous', 'runtime': 'native', 'argv_shape': [], 'config_dirs': [],
                       'compatibility': observe(str(self.exe), [str(self.exe)], str(self.root))}
        self.recipe = {'agent_identity_name': 'evidence-name', 'match_features': {'runtime': 'native'},
                       'hook': {'method': 'unsupported', 'restart_required': 'unknown', 'capabilities': [],
                                'verification': 'pending', 'rollback': 'none installed', 'limitations': ['unknown']},
                       'observation': 'observed', 'fallback': 'manual', 'evidence_refs': ['ev-1-0123456789']}
        self.evidence = [{'evidence_id': 'ev-1-0123456789', 'tool': 'get_target_context'}]
        self.TARGET = {'pid': 7, 'create_time': 123.0}

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def test_credential_mask_never_exposes_suffix(self):
        masked = mask_key('secret-value-with-sensitive-suffix')
        self.assertEqual(masked, 'present(len=hidden)')
        self.assertNotIn('suffix', masked)

    def _write_evidence(self, tool='get_target_context', result=None, target=None):
        ev = {'tool': tool, 'error': None,
              'result': result if result is not None else {'target': dict(target or self.TARGET), 'local_evidence': {'pid': 7}},
              'target': dict(target or self.TARGET)}
        (self.root / 'ev-1-0123456789.json').write_text(json.dumps(ev))

    # ---------- 原生二进制：不强制 entry_token；changed 后只能 similar ----------
    def test_native_exact_readonly_and_changed_binary_similar(self):
        evidence = [dict(e, target=dict(self.TARGET)) for e in self.evidence]
        entry = matcher.remember_verified(self.struct, self.recipe, evidence)
        before = matcher.db_path().stat().st_mtime_ns
        self.assertEqual(matcher.classify(self.struct)['status'], 'exact')
        self.assertEqual(matcher.db_path().stat().st_mtime_ns, before)  # 纯读
        self.exe.write_bytes(b'native build two changed')
        changed = dict(self.struct, compatibility=observe(str(self.exe), [str(self.exe)], str(self.root)))
        self.assertEqual(matcher.classify(changed)['status'], 'similar')
        self.assertFalse(entry['hook_verified'])

    def test_entry_change_and_unknown_are_not_exact(self):
        node = self.root / 'node'; node.write_bytes(b'node binary')
        first = self.root / 'one.js'; first.write_text('first')
        second = self.root / 'two.js'; second.write_text('second')
        a = dict(self.struct, exe='node', runtime='node',
                 compatibility=observe(str(node), [str(node), str(first)], str(self.root)))
        E = [dict(e, target=dict(self.TARGET)) for e in self.evidence]
        matcher.remember_verified(a, self.recipe, E)
        b = dict(a, compatibility=observe(str(node), [str(node), str(second)], str(self.root)))
        self.assertEqual(matcher.classify(b)['status'], 'similar')
        self.assertEqual(matcher.classify(dict(a, compatibility=None))['status'], 'similar')
        self.assertEqual(matcher.classify(dict(a, exe='other'))['status'], 'miss')

    def test_legacy_never_exact(self):
        matcher.remember(self.struct, self.recipe, 1)
        self.assertEqual(matcher.classify(self.struct)['status'], 'similar')

    def test_concurrent_revisions_keep_history(self):
        E = [dict(e, target=dict(self.TARGET)) for e in self.evidence]
        entry = matcher.remember_verified(self.struct, self.recipe, E)
        recipe = dict(self.recipe, match_features={'evolves_prior_harness': entry['id']})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: matcher.remember_verified(self.struct, recipe, E), range(8)))
        data = matcher.load()['fingerprints'][0]
        self.assertEqual([r['revision'] for r in data['revisions']], list(range(1, 10)))
        self.assertEqual(data['revisions'][0]['recipe'], self.recipe)

    # ---------- 配方校验：结构 ≠ 证据支持；绑定目标实例；真实成功结果 ----------
    def test_validate_ok_and_structure_only(self):
        self._write_evidence()
        result = validate(self.recipe, self.root, target=self.TARGET)
        self.assertEqual(result['evidence'], self.evidence)
        # 结构校验通过只证明证据存在且绑定实例；接入点一律 proposed/unverified。
        self.assertFalse(result['hook_evidence_supported'])
        # 任意 method（含虚构/明确端点）都不能得到 supported：没有可核对的映射。
        for method in ('local-listening-port', 'magic-ld-preload', 'sitecustomize-v2', 'ld_preload-x'):
            rec = dict(self.recipe, hook=dict(self.recipe['hook'], method=method))
            out = validate(rec, self.root, target=self.TARGET)
            self.assertEqual(out['evidence'], self.evidence)
            self.assertFalse(out['hook_evidence_supported'])

    def test_validate_rejects_unbound_or_empty_evidence(self):
        # 无 result 内容 → 拒绝
        self._write_evidence(result={'status': 'ok'})
        with self.assertRaises(ValueError):
            validate(self.recipe, self.root, target=self.TARGET)
        # 无 target 绑定字段 → 拒绝
        ev = {'tool': 'get_target_context', 'error': None, 'result': {'target': {'pid': 7, 'create_time': 123.0}}}
        (self.root / 'ev-1-0123456789.json').write_text(json.dumps(ev))
        with self.assertRaises(ValueError):
            validate(self.recipe, self.root, target=self.TARGET)
        # 绑定不一致（PID 复用/跨实例）→ 拒绝
        self._write_evidence(result={'target': dict(self.TARGET)})
        with self.assertRaises(ValueError):
            validate(self.recipe, self.root, target={'pid': 7, 'create_time': 124.0})
        with self.assertRaises(ValueError):
            validate(self.recipe, self.root, target={'pid': 8, 'create_time': 123.0})
        # 证据文件不存在
        (self.root / 'ev-1-0123456789.json').unlink()
        with self.assertRaises(FileNotFoundError):
            validate(self.recipe, self.root, target=self.TARGET)

    def test_read_related_file_evidence_requires_binding_and_real_read(self):
        # read_related_file 被许可为证据类型 ≠ 任何文件都证明任意结论：
        # 每条引用仍必须绑定本次冻结实例，且读取结果非空、无 error。
        ctx_ev = {'tool': 'get_target_context', 'error': None,
                  'result': {'target': dict(self.TARGET), 'local_evidence': {'pid': 7}},
                  'target': dict(self.TARGET)}
        (self.root / 'ev-2-0123456789.json').write_text(json.dumps(ctx_ev))
        read_ev = {'tool': 'read_related_file', 'error': None,
                   'result': {'path': '/fixture/target/server.py',
                              'content': 'import time', 'parse_status': 'text'},
                   'target': dict(self.TARGET)}
        (self.root / 'ev-1-0123456789.json').write_text(json.dumps(read_ev))
        recipe = dict(self.recipe, evidence_refs=['ev-1-0123456789', 'ev-2-0123456789'])
        out = validate(recipe, self.root, target=self.TARGET)
        self.assertEqual(len(out['evidence']), 2)
        self.assertFalse(out['hook_evidence_supported'])
        self.assertEqual(out['evidence'][0]['tool'], 'read_related_file')
        # 错误绑定（另一实例）拒绝
        (self.root / 'ev-1-0123456789.json').write_text(json.dumps(
            dict(read_ev, target={'pid': 7, 'create_time': 999.0})))
        with self.assertRaises(ValueError):
            validate(recipe, self.root, target=self.TARGET)
        # 读取失败（error 非空）拒绝
        (self.root / 'ev-1-0123456789.json').write_text(json.dumps(
            dict(read_ev, target=dict(self.TARGET), error='read_failed')))
        with self.assertRaises(ValueError):
            validate(recipe, self.root, target=self.TARGET)
        # 空结果拒绝（read 成功但无任何内容字段时不作为有效证据）
        (self.root / 'ev-1-0123456789.json').write_text(json.dumps(
            dict(read_ev, target=dict(self.TARGET), result={})))
        with self.assertRaises(ValueError):
            validate(recipe, self.root, target=self.TARGET)

    # ---------- 演进门禁：同 exe/runtime 不足；需要入口/包身份证据 ----------
    def test_evolution_requires_family_identity_evidence(self):
        E = [dict(e, target=dict(self.TARGET)) for e in self.evidence]
        entry = matcher.remember_verified(self.struct, self.recipe, E)
        evolve = dict(self.recipe, match_features={'evolves_prior_harness': entry['id']})
        # 共享 node/python：同 exe+runtime 但入口不同 → 拒绝演进（similar 只是调查参考）
        node = self.root / 'node'; node.write_bytes(b'node binary')
        a = self.root / 'a.js'; a.write_text('aaa')
        b = self.root / 'b.js'; b.write_text('bbb')
        shared = dict(self.struct, exe='node', runtime='node',
                      compatibility=observe(str(node), [str(node), str(a)], str(self.root)))
        with self.assertRaises(ValueError):
            matcher.remember_verified(shared, evolve, E)
        # 同可执行 + 同入口路径（内容演进）→ 允许演进，产生新 revision
        same_entry = dict(self.struct, compatibility=observe(str(self.exe), [str(self.exe)], str(self.root)))
        evolved = matcher.remember_verified(same_entry, evolve, E)
        self.assertEqual(evolved['id'], entry['id'])
        revs = matcher.load()['fingerprints'][0]['revisions']
        self.assertEqual([r['revision'] for r in revs], [1, 2])
        # 模型给不存在的旧 id → 拒绝合并
        bad = dict(self.recipe, match_features={'evolves_prior_harness': 'no-such-id'})
        with self.assertRaises(ValueError):
            matcher.remember_verified(self.struct, bad, E)

    # ---------- install_plan 门禁：真实生成内容 + 随机目录；穿越/改文件缺摘要/方法不匹配拒绝 ----------
    def test_install_plan_positive_and_negative(self):
        import hashlib
        import random
        self._write_evidence()
        # 人工标注的测试样本：随机目录名由本测试固定种子生成，不代表自主生成成功。
        rng = random.Random(20260910)
        token = lambda: 'asg-fixture-' + ''.join(rng.choice('0123456789abcdef') for _ in range(12))
        dir_a, dir_b, existing_dir = token(), token(), token()
        existing = existing_dir + '/config.json'
        plan = {
            'version': 1,
            'files': [
                {'path': dir_a + '/hook.js', 'content': 'export const id = "learned";\n', 'expected_sha256': None},
                {'path': dir_b + '/README.md', 'content': '# learned plan fixture\n', 'expected_sha256': None},
                {'path': existing, 'content': '{"hooked": true}',
                 'expected_sha256': hashlib.sha256(b'{}').hexdigest()},
            ],
        }
        recipe = dict(self.recipe, hook=dict(self.recipe['hook'], method='file_plan'), install_plan=plan)
        out = validate(recipe, self.root, target=self.TARGET)
        self.assertEqual(len(out['evidence']), 1)
        self.assertFalse(out['hook_evidence_supported'])
        # 负例 1：路径穿越
        escape = dict(plan)
        escape['files'] = [dict(plan['files'][0], path='../../../etc/asg-escape')]
        with self.assertRaises(ValueError):
            validate(dict(recipe, install_plan=escape), self.root, target=self.TARGET)
        # 负例 2：修改已存在文件但缺 expected_sha256 —— 门禁只做结构校验（无文件系统
        # 语义），"修改必须带原内容摘要"由安装原语的 precondition 强制（learned_install
        # 为顾问所有，本测试只消费其公开接口）。
        from runtime import learned_install as li
        import tempfile
        with tempfile.TemporaryDirectory() as ws_tmp:
            ws_root = Path(ws_tmp).resolve()
            ws = ws_root / 'workspace'; ws.mkdir()
            state = ws_root / 'state'; state.mkdir()
            target_file = ws / existing
            target_file.parent.mkdir(); target_file.write_text('{}')
            missing_digest = {'version': 1, 'files': [dict(plan['files'][2], expected_sha256=None)]}
            with self.assertRaises(ValueError):
                li.install(missing_digest, ws, state,
                           approved_workspace=ws, approved_digest=li.plan_digest(missing_digest))
            self.assertEqual(target_file.read_text(), '{}')
        # 负例 3：install_plan 与 hook.method 不匹配
        with self.assertRaises(ValueError):
            validate(dict(recipe, hook=dict(self.recipe['hook'], method='magic')), self.root, target=self.TARGET)

    # ---------- 采集：成功项保留 + 局部失败；本地读取 vs 脱敏外发 ----------
    def _proc(self, files=None):
        p = Mock()
        p.pid = 7; p.exe.return_value = str(self.exe); p.name.return_value = 'anonymous'
        p.cmdline.return_value = [str(self.exe)]; p.cwd.return_value = str(self.root)
        p.open_files.return_value = [Mock(path=str(f)) for f in (files or [])]
        p.net_connections.return_value = []
        return p

    def test_collection_keeps_successes_on_partial_failure(self):
        good = self.root / 'config.json'
        good.write_text(json.dumps({'model': 'm-good', 'mcpServers': {'x': {}}}))
        bad = self.root / 'settings.json'; bad.write_text('{broken')
        result = collect(self._proc([good, bad]))
        # 局部失败保留成功项
        self.assertEqual(result['assets']['model_routing']['status'], 'collected')
        self.assertEqual(result['assets']['model_routing']['value']['model'], 'm-good')
        self.assertEqual(result['assets']['registered_tools_and_mcp']['value'], ['x'])
        self.assertIn('部分配置解析失败', result['assets']['parsed_config']['message'])
        self.assertEqual(result['assets']['parsed_config']['status'], 'collected')
        # 全部失败才 failed
        good.write_text('{bad')  # 之前成功的 config.json 也损坏
        bad2 = self.root / 'config2.json'; bad2.write_text('{bad')
        allbad = collect(self._proc([bad, bad2]))
        self.assertEqual(allbad['assets']['parsed_config']['status'], 'failed')
        # limitations 区分本地读取与外发：不出现 "No raw config read" 的虚假声明
        joined = ' '.join(allbad['limitations'])
        self.assertIn('本机读取', joined)
        self.assertIn('只读不导出', joined)
        self.assertNotIn("No raw config", joined)

    def test_collection_denies_secrets_and_marks_network_fail(self):
        cfg = self.root / 'config.json'
        cfg.write_text(json.dumps({'model': 'm', 'apiKey': 'never-export-this', 'instructions': []}))
        p = self._proc([cfg])
        p.net_connections.side_effect = psutil.AccessDenied()
        result = collect(p)
        self.assertNotIn('never-export-this', json.dumps(result))
        self.assertEqual(result['assets']['network_surface']['status'], 'failed')
        self.assertIn('观测未完成', result['assets']['network_surface']['message'])

    def test_proxy_normalizes_goose_v1(self):
        from runtime.llm_proxy import _ProxyHandler, _STATE
        from io import BytesIO
        handler = object.__new__(_ProxyHandler)
        handler.path = '/v1/chat/completions'; handler.headers = {'Content-Length': '2'}
        handler.rfile = BytesIO(b'{}'); handler.wfile = BytesIO()
        handler.send_response = Mock(); handler.send_header = Mock(); handler.end_headers = Mock()
        upstream = Mock(status_code=200, headers={}); upstream.iter_content.return_value = []
        with patch.dict(_STATE, base_url='https://example.invalid/model/v1', origin='https://example.invalid', key='test-only'), \
             patch('runtime.llm_proxy.requests.post', return_value=upstream) as post:
            handler.do_POST()
            self.assertEqual(post.call_args.args[0], 'https://example.invalid/model/v1/chat/completions')
            self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_tls_exception_is_config_driven_for_any_custom_route(self):
        route = {'route': 'any-provider', 'provider': 'openai', 'model': 'model-a',
                 'base_url': 'https://llm.example.test/v1', 'tls': {'verify': False}}
        self.assertTrue(tls_exception_enabled(route))
        with patch('runtime.llm_proxy.ensure_proxy', return_value='http://127.0.0.1:43111'):
            self.assertEqual(goose_env(route, 'test-only')['OPENAI_BASE_URL'], 'http://127.0.0.1:43111')

        verified = dict(route, base_url='https://other.example/v1', tls={'verify': True})
        self.assertFalse(tls_exception_enabled(verified))
        self.assertEqual(goose_env(verified, 'test-only')['OPENAI_BASE_URL'], 'https://other.example/v1')

    def test_legacy_flag_is_only_fallback_and_does_not_override_explicit_verify(self):
        with patch.dict(os.environ, {'ASG_INSECURE_SSL': '1'}):
            legacy = {'route': 'legacy-provider', 'base_url': 'https://legacy.example/v1'}
            self.assertTrue(tls_exception_enabled(legacy))
            verified = {'route': 'verified-provider', 'base_url': 'https://verified.example/v1',
                        'tls': {'verify': True}}
            self.assertFalse(tls_exception_enabled(verified))

    def test_proxy_rejects_cross_origin_reconfiguration(self):
        import runtime.llm_proxy as proxy
        with patch.object(proxy, '_SERVER', object()), patch.dict(proxy._STATE, origin='https://first.example'):
            route = {'route': 'second', 'base_url': 'https://second.example/v1',
                     'tls': {'verify': False}}
            with self.assertRaises(ValueError):
                proxy.ensure_proxy(route, 'test-only')



    def test_family_identity_shell_vs_native_and_shared_runtime(self):
        # 同壳不同 app（同一 Electron 二进制，不同 .app 包）→ 不合并
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exeA = root / 'ElectronA.app' / 'Contents' / 'MacOS' / 'Electron'
            exeA.parent.mkdir(parents=True); exeA.write_bytes(b'BIN-A')
            compatA = observe(str(exeA), [str(exeA)], str(root))
            exeB = root / 'ElectronB.app' / 'Contents' / 'MacOS' / 'Electron'
            exeB.parent.mkdir(parents=True); exeB.write_bytes(b'BIN-B')
            compatB = observe(str(exeB), [str(exeB)], str(root))
            self.assertFalse(matcher._family_identity_matches(compatB, compatA))
            # 同 app 原生升级：exe digest 变，包名一致 → 同族演进允许
            exeA2 = root / 'ElectronA.app' / 'Contents' / 'MacOS' / 'Electron'
            exeA2.write_bytes(b'BIN-A-UPDATED')
            compatA2 = observe(str(exeA2), [str(exeA2)], str(root))
            self.assertTrue(matcher._family_identity_matches(compatA2, compatA))
            # 共享 node/python：同解释器不同脚本入口 → 不合并
            node = root / 'node'; node.write_bytes(b'NODE')
            a = root / 'a.js'; a.write_text('AAA')
            b = root / 'b.js'; b.write_text('BBB')
            ca = observe(str(node), [str(node), str(a)], str(root))
            cb = observe(str(node), [str(node), str(b)], str(root))
            self.assertFalse(matcher._family_identity_matches(cb, ca))
            # 同脚本升级（basename 一致，内容变）→ 同族
            a2 = root / 'a.js'; a2.write_text('AAAv2')
            ca2 = observe(str(node), [str(node), str(a2)], str(root))
            self.assertTrue(matcher._family_identity_matches(ca2, ca))


if __name__ == '__main__':
    unittest.main()
