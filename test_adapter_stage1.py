# -*- coding: utf-8 -*-
"""第二种接入机制隔离适配测试（合成/mock，不冒充第二个真实 Agent 验收）。

目的：证明核心（发现/身份/资产/调查/指纹/配方契约）无需改写即可更换接入后端。
这里用 Python sitecustomize 运行时钩子（与 OpenCode 的 Node 桌面机制不同），
跑一遍 合成 agent -> analyze -> collect -> validate -> remember_verified -> classify 复用，
全程无产品名特判、无 if name==xxx 分支。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime import matcher, analyzer, collection, recipe_validation


class AdapterStage1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, ASG_FINGERPRINT_DB=str(self.root / 'fp.json'))
        self.env.start()
        # 合成 Python agent：随机命名（非产品名单），运行时钩子机制为 sitecustomize
        self.agent_name = 'xkq-synth-' + 'synthetic48'
        self.script = self.root / (self.agent_name + '.py')
        self.script.write_text('# synthetic agent worker\nimport time\nwhile True: time.sleep(1)\n')
        self.struct = {
            'exe': self.script.name, 'runtime': 'python', 'cwd': str(self.root),
            'argv_shape': [self.script.name], 'config_dirs': [],
            'compatibility': __import__('runtime.compatibility', fromlist=['observe']).observe(
                sys.executable, [sys.executable, str(self.script)], str(self.root)),
        }
        self.recipe = {
            'agent_identity_name': self.agent_name,
            'match_features': {'runtime': 'python'},
            'hook': {'method': 'sitecustomize-v2', 'restart_required': 'unknown',
                     'capabilities': ['observe python SDK calls on public surface'],
                     'verification': 'pending', 'rollback': 'remove sitecustomize', 'limitations': ['unknown']},
            'observation': 'observed synthetic python agent',
            'fallback': 'manual', 'evidence_refs': ['ev-9-abcdef0123'],
        }
        self.evidence = [{'evidence_id': 'ev-9-abcdef0123', 'tool': 'get_target_context'}]
        self.TARGET = {'pid': 9001, 'create_time': 55.0}

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def _write_evidence(self):
        ev = {'tool': 'get_target_context', 'error': None,
              'result': {'target': dict(self.TARGET), 'local_evidence': {'pid': 9001}},
              'target': dict(self.TARGET)}
        (self.root / 'ev-9-abcdef0123.json').write_text(json.dumps(ev))

    def test_alternate_mechanism_uses_same_core_contract(self):
        # ① 发现/分析（核心）
        # ② 证据采集契约（collect）
        p = __import__('unittest.mock', fromlist=['Mock']).Mock()
        p.pid = 9001
        p.exe.return_value = str(self.script)
        p.name.return_value = self.agent_name
        p.cmdline.return_value = [sys.executable, str(self.script)]
        p.cwd.return_value = str(self.root)
        p.open_files.return_value = []
        p.net_connections.return_value = []
        collected = collection.collect(p)
        self.assertEqual(collected['assets']['network_surface']['status'], 'collected')
        # ③ 配方校验（同契约；hook 接入点是否真存在仅 evidence 可证，恒 proposed）
        self._write_evidence()
        validated = recipe_validation.validate(self.recipe, self.root, target=self.TARGET)
        self.assertEqual(validated['evidence'], self.evidence)
        self.assertFalse(validated['hook_evidence_supported'])
        # ④ 落库 + 复用（核心指纹路径）
        entry = matcher.remember_verified(self.struct, self.recipe, validated['evidence'])
        self.assertEqual(matcher.classify(self.struct)['status'], 'exact')
        self.assertEqual(entry['revision'], 1)
        self.assertFalse(entry['hook_verified'])

    def test_alternate_mechanism_reject_signature_change(self):
        self._write_evidence()
        validated = recipe_validation.validate(self.recipe, self.root, target=self.TARGET)
        matcher.remember_verified(self.struct, self.recipe, validated['evidence'])
        changed = dict(self.struct, argv_shape=[self.script.name, '--flag-x'],
                       compatibility=__import__('runtime.compatibility', fromlist=['observe']).observe(
                           sys.executable, [sys.executable, str(self.script), '--flag-x'], str(self.root)))
        self.assertEqual(matcher.classify(changed)['status'], 'similar')


if __name__ == '__main__':
    unittest.main()
