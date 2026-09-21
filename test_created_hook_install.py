import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from runtime import protocol_fastpath


class CreatedHookInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='asg-create-hooks-')).resolve()
        self.workspace = self.tmp / 'proj'
        (self.workspace / '.th AppConfig').mkdir(parents=True)
        (self.workspace / 'app').mkdir(parents=True)
        # Host capability evidence: quoted vocabulary + type/command declaration.
        (self.workspace / 'app' / 'host.js').write_text(
            'const A = "BeforeTool"; const B = "AfterTool"; const C = "SessionStart";'
            'const d = {"type": "command", "command": x};')
        self.config = self.workspace / '.th AppConfig' / 'settings.json'
        self.config.write_text(json.dumps({'theme': {'dark': True}}))
        self.root = self.tmp / 'state'
        self.root.mkdir()
        env = {'ASG_ONBOARDING_AUTHORIZED': '1', 'ASG_ONBOARDING_AUTO_INSTALL': '1',
               'ASG_ONBOARDING_SCOPE': 'project',
               'ASG_ONBOARDING_WORKSPACE_ROOTS': str(self.workspace)}

        import psutil
        self.process = psutil.Process()
        self.target = {'pid': os.getpid(), 'create_time': self.process.create_time()}
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._surf = mock.patch('runtime.analyst_evidence.entry_surface',
                               return_value={'related_roots': [{'path': str(self.workspace / 'app')}]})
        self._surf.start()
        self.addCleanup(self._surf.stop)
        self._live = mock.patch('runtime.onboarding._target_is_live')
        self._live.start()
        self.addCleanup(self._live.stop)

    def _candidate(self):
        surface = {'checked_paths': [str(self.config)]}
        found, why = protocol_fastpath._creation_candidate(self.process, surface)
        return found, why

    def test_capability_probe_is_structured(self):
        caps = protocol_fastpath.host_hook_capability([self.workspace / 'app'])
        self.assertIsNotNone(caps)
        self.assertIn('BeforeTool', caps['events'])
        missing = protocol_fastpath.host_hook_capability([self.tmp / 'empty'])
        self.assertIsNone(missing)

    def test_dialect_selection(self):
        found, why = self._candidate()
        self.assertIsNotNone(found, why)
        self.assertEqual(found['create']['dialect'], 'alias')
        self.assertIn('BeforeTool', found['create']['events'])

    def test_install_writes_valid_grouped_structure_and_rollback_restores(self):
        original = self.config.read_bytes()
        found, why = self._candidate()
        self.assertIsNotNone(found, why)
        with mock.patch('runtime.autonomous_pipeline.path', return_value=self.root / 'pipeline.json'):
            result = protocol_fastpath.install(
                found['config'], self.target, self.root, ['python3', 'control'],
                create=found['create'])
        self.assertEqual(result['status'], 'installed')
        self.assertTrue(result['hook_structure_created'])
        data = json.loads(self.config.read_text())
        entries = data['hooks']['BeforeTool']
        self.assertEqual(entries[0]['matcher'], '*')
        self.assertEqual(entries[0]['hooks'][0]['type'], 'command')
        from runtime.integration_protocol import detect
        self.assertTrue(any(c['family'] == 'command_hooks' and c['pointer'] == '/hooks'
                            for c in detect(data)))
        config_dir = found['config'].parent
        protocol_fastpath.learned_install.rollback(
            config_dir, protocol_fastpath.Path(result['state_dir']),
            approved_workspace=config_dir, approved_digest=result['plan_digest'])
        self.assertEqual(self.config.read_bytes(), original)

    def test_existing_hooks_key_is_never_overwritten(self):
        self.config.write_text(json.dumps({'hooks': {'BeforeTool': []}}))
        found, why = self._candidate()
        self.assertIsNone(found)
        self.assertIn('唯一可写 JSON 配置', why)

    def test_no_capability_evidence_blocks_creation(self):
        shutil.rmtree(self.workspace / 'app')
        found, why = self._candidate()
        self.assertIsNone(found)
        self.assertIn('command-Hook', why)
