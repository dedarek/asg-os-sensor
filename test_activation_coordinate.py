# -*- coding: utf-8 -*-
"""End-to-end coordination test on a disposable workspace and a real throwaway
process.

This is NOT a real Agent Hook acceptance test. It exercises the generic
coordination entry against a temporary workspace, a synthetic candidate and a
short-lived local process, so the code path is proven without touching any
desktop Agent, the dashboard or a live investigation instance.
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime import learned_onboarding as onboarding
from runtime import observation_source as src

BUILD = {'executable': 'exe-digest-same', 'entry': 'entry-digest-same',
         'platform': 'Darwin', 'architecture': 'arm64', 'runtime': 'python',
         'entry_path': '/ws/worker.py'}
DECLARATION = {'log_path': 'logs/probe.jsonl',
               'fields': {'event': 'kind', 'pid': 'process', 'timestamp': 'at'}}


def _spawn_worker() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])


class CoordinateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # The real installer rejects symlinks anywhere in a supervisor path, and
        # macOS temp dirs are reached through the /var symlink, so resolve once.
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / 'ws'
        (self.workspace / 'logs').mkdir(parents=True)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        self.worker = _spawn_worker()
        import psutil
        self.target = {'pid': self.worker.pid,
                       'create_time': psutil.Process(self.worker.pid).create_time()}

    def tearDown(self):
        if self.worker.poll() is None:
            self.worker.terminate()
            self.worker.wait(timeout=10)
        self._tmp.cleanup()

    def recipe(self, declaration=DECLARATION):
        recipe = {'agent_identity_name': 'fixture-worker',
                  'match_features': {'runtime': 'python'},
                  'observation': 'fixture', 'fallback': 'fixture',
                  "hook": {"method": "file_plan", "workspace": str(self.workspace),
                           "restart_required": True, "capabilities": ["observe"],
                           "verification": "v", "rollback": "r", "limitations": []},
                  "evidence_refs": ["ev-1-0123456789"],
                  "install_plan": {"version": 1, "files": [
                      {"path": "hook-probe.py", "content": "print(1)\n",
                       "expected_sha256": None}]}}
        if declaration is not None:
            recipe["observation_source"] = declaration
        return recipe

    def scope(self, *, install=True, rebind=False):
        return onboarding.authorization_scope(approved_workspace=self.workspace,
                                              allow_install=install, allow_rebind=rebind)

    def coordinate(self, **overrides):
        kwargs = {'recipe': self.recipe(), 'evidence_dir': self.evidence,
                  'target': self.target, 'workspace': self.workspace,
                  'state_dir': self.state, 'authorization': self.scope(),
                  'observed_compatibility': BUILD}
        kwargs.update(overrides)
        with patch.object(onboarding, 'validate',
                          return_value={'evidence': [], 'hook_evidence_supported': False}), \
             patch.object(onboarding.psutil, 'Process') as proc:
            proc.return_value.create_time.return_value = kwargs['target']['create_time']
            return onboarding.coordinate(**kwargs)

    def test_full_path_installs_then_binds_without_restarting_anything(self):
        result = self.coordinate()

        self.assertEqual(result['status'], 'installed')
        self.assertFalse(result['restarted_target'])
        self.assertTrue(Path(result['config_path']).is_file())
        binding = src.load_config(Path(result['config_path']))
        self.assertEqual(binding['target'], self.target)
        self.assertEqual(binding['fields'], {'event': 'kind', 'pid': 'process', 'timestamp': 'at'})
        self.assertEqual(binding['log_path'], str(self.workspace / 'logs' / 'probe.jsonl'))
        # The installed hook file exists in the workspace, not in state.
        self.assertTrue((self.workspace / 'hook-probe.py').is_file())
        # The throwaway process is untouched by the coordinator.
        self.assertIsNone(self.worker.poll())

    def test_second_call_is_idempotent(self):
        first = self.coordinate()
        second = self.coordinate()
        self.assertEqual(first['status'], 'installed')
        self.assertEqual(second['status'], 'bound')
        self.assertEqual(second['config_path'], first['config_path'])
        self.assertEqual(src.load_config(Path(second['config_path']))['target'], self.target)

    def test_without_install_authorization_nothing_is_written(self):
        result = self.coordinate(authorization=self.scope(install=False))
        self.assertEqual(result['status'], 'pending_authorization')
        self.assertFalse((self.workspace / 'hook-probe.py').exists())
        self.assertFalse((self.state / onboarding.OBSERVATION_FILE).exists())
        self.assertEqual(result['required_scope'], {'install': True})

    def test_restarted_target_requires_authorized_rebind(self):
        self.coordinate()
        new_worker = _spawn_worker()
        try:
            import psutil
            new_target = {'pid': new_worker.pid,
                          'create_time': psutil.Process(new_worker.pid).create_time()}
            # Not authorized: reports the need, writes nothing.
            pending = self.coordinate(target=new_target,
                                      authorization=self.scope(install=True, rebind=False))
            self.assertEqual(pending['status'], 'rebind_required')
            self.assertEqual(pending['bound_target'], self.target)
            self.assertEqual(src.load_config(Path(pending['config_path']))['target'], self.target)

            # Authorized with build evidence: the binding migrates, no reinstall.
            manifest = self.state / (pending['plan_digest'] + '.json')
            before = manifest.read_bytes()
            moved = self.coordinate(target=new_target, evidence=['verified same build'],
                                    authorization=self.scope(install=True, rebind=True))
            self.assertEqual(moved['status'], 'rebound')
            self.assertEqual(moved['previous_target'], self.target)
            self.assertEqual(src.load_config(Path(moved['config_path']))['target'], new_target)
            # The install manifest is byte-identical: no reinstall happened.
            self.assertEqual(manifest.read_bytes(), before)
            history = json.loads(Path(moved['history_path']).read_text(encoding='utf-8'))
            self.assertEqual([item['kind'] for item in history['bindings']],
                             ['initial', 'activation_rebind'])
            self.assertEqual(history['bindings'][0]['target'], self.target)
        finally:
            if new_worker.poll() is None:
                new_worker.terminate()
                new_worker.wait(timeout=10)

    def test_rebind_is_not_performed_without_build_evidence(self):
        self.coordinate()
        new_worker = _spawn_worker()
        try:
            import psutil
            new_target = {'pid': new_worker.pid,
                          'create_time': psutil.Process(new_worker.pid).create_time()}
            with self.assertRaises(ValueError):
                self.coordinate(target=new_target, evidence=None,
                                authorization=self.scope(install=True, rebind=True))
            self.assertEqual(src.load_config(self.state / onboarding.OBSERVATION_FILE)['target'],
                             self.target)
        finally:
            if new_worker.poll() is None:
                new_worker.terminate()
                new_worker.wait(timeout=10)

    def test_undeclared_candidate_installs_without_a_binding(self):
        result = self.coordinate(recipe=self.recipe(declaration=None))
        self.assertEqual(result['status'], 'installed_no_observation')
        self.assertIsNone(result['config_path'])
        self.assertFalse((self.state / onboarding.OBSERVATION_FILE).exists())

    def test_workspace_outside_the_approved_scope_is_refused(self):
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaises(PermissionError):
            self.coordinate(authorization=onboarding.authorization_scope(
                approved_workspace=other, allow_install=True))
        self.assertFalse((other / 'hook-probe.py').exists())
        self.assertFalse((self.workspace / 'hook-probe.py').exists())


if __name__ == '__main__':
    unittest.main()
