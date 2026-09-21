# -*- coding: utf-8 -*-
"""Exact file_plan reuse: original provenance, installed-state rebind, refusals.

Not a real Agent Hook acceptance test. It uses a disposable workspace and real
short-lived local processes, with explicitly synthetic investigation evidence
whose only purpose is to carry a target binding. Nothing mocks validate(), the
build comparison or process liveness.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from runtime import learned_onboarding, matcher, onboarding
from runtime.recipe_validation import validate

REF = 'ev-1-0123456789'
BUILD = {'executable': 'exe-a', 'entry': 'native', 'entry_path': '/ws/worker.py',
         'platform': 'Darwin', 'architecture': 'arm64', 'runtime': 'native'}
DECLARATION = {'log_path': 'logs/probe.jsonl',
               'fields': {'event': 'kind', 'pid': 'process', 'timestamp': 'at'}}


class ExactReuseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # The real installer rejects symlinks in a supervisor path and macOS
        # temporary directories are reached through /var, so resolve once.
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / 'ws'
        (self.workspace / 'logs').mkdir(parents=True)
        self.run_root = self.root / 'runs'
        self.run_dir = self.run_root / 'pid_1000_1'
        self.evidence_dir = self.run_dir / 'evidence'
        self.evidence_dir.mkdir(parents=True)
        self.state = self.run_dir / 'coordinator-state'
        # The real installer requires the supervisor state directory to exist.
        self.state.mkdir(parents=True)
        self.processes = []
        self.env = patch.dict(os.environ, {
            'ASG_RUN_DIR': str(self.run_root),
            'ASG_FINGERPRINT_DB': str(self.root / 'fp.json'),
            'ASG_EXPERIENCE_DB': str(self.root / 'experience.json'),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self._tmp.cleanup()

    def live_target(self):
        """A real throwaway process; its live pid+create_time are the target."""
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.processes.append(proc)
        return {'pid': proc.pid, 'create_time': psutil.Process(proc.pid).create_time()}

    def write_evidence(self, target):
        (self.evidence_dir / (REF + '.json')).write_text(json.dumps({
            'evidence_id': REF, 'tool': 'get_target_context', 'error': None,
            'target': dict(target),
            'result': {'target': dict(target), 'local_evidence': {'pid': target['pid']},
                       'synthetic': 'test-only binding, not a real investigation'},
        }), encoding='utf-8')

    def recipe(self):
        return {'agent_identity_name': 'fixture-worker',
                'match_features': {'runtime': 'python'},
                'observation': 'fixture', 'fallback': 'fixture',
                'hook': {'method': 'file_plan', 'workspace': str(self.workspace),
                         'restart_required': True, 'capabilities': ['observe'],
                         'verification': 'v', 'rollback': 'r', 'limitations': []},
                'evidence_refs': [REF], 'observation_source': DECLARATION,
                'install_plan': {'version': 1, 'files': [
                    {'path': 'hook-probe.py', 'content': 'print(1)\n',
                     'expected_sha256': None}]}}

    def struct(self, target):
        return {'pid': target['pid'], 'create_time': target['create_time'],
                'exe': 'fixture-worker', 'runtime': 'python',
                'argv_shape': ['fixture-worker', 'worker.py'], 'config_dirs': [],
                'exe_full': '/tmp/fixture-worker', 'cwd': str(self.workspace),
                'compatibility': dict(BUILD)}

    def install_once(self, target):
        """Real coordinator install: real validate, manifest, prepared record."""
        return learned_onboarding.coordinate(
            self.recipe(), self.evidence_dir, target, workspace=self.workspace,
            state_dir=self.state,
            authorization=learned_onboarding.authorization_scope(
                approved_workspace=str(self.workspace), allow_install=True),
            observed_compatibility=dict(BUILD))

    def register_fingerprint(self, target):
        """Record the validated recipe so classify() can return exact later."""
        evidence = validate(self.recipe(), self.evidence_dir, target=target)['evidence']
        return matcher.remember_verified(self.struct(target), self.recipe(), evidence, source='goose')

    def reuse_plan(self, target):
        match = matcher.classify(self.struct(target))
        self.assertEqual(match['status'], 'exact', match)
        return onboarding.plan_from_match(self.struct(target), match)

    def test_exact_reuse_of_predetermined_install_rebinds_the_restarted_instance(self):
        first = self.live_target()
        self.write_evidence(first)
        installed = self.install_once(first)
        self.assertEqual(installed['status'], 'installed', installed)
        self.register_fingerprint(first)
        manifest = self.state / (installed['plan_digest'] + '.json')
        manifest_before = manifest.read_bytes()

        # Restart: the old instance goes away and a new one takes its place.
        self.processes[0].terminate()
        self.processes[0].wait(timeout=10)
        second = self.live_target()

        plan = self.reuse_plan(second)
        self.assertEqual(plan['adapter'], 'file_plan')
        self.assertEqual(plan['action'], 'install_reused_recipe')
        self.assertIsInstance(plan.get('reuse_recipe'), dict)
        self.assertEqual(plan.get('reuse_evidence_refs'), [REF])
        self.assertEqual(plan.get('observed_compatibility'), BUILD)
        # The plan must not claim to have run a fresh investigation.
        self.assertNotIn('investigation_run_dir', plan)

        with patch.dict(os.environ, {'ASG_ONBOARDING_REBIND': '1'}):
            result = onboarding.execute_install(
                plan, second,
                {'approved': True, 'scope': 'project', 'workspace': str(self.workspace)})

        # A restart must migrate the binding, not report a stale no-op.
        self.assertEqual(result['status'], 'rebound', result)
        self.assertEqual(result['previous_target'], first)
        binding = json.loads((self.state / learned_onboarding.OBSERVATION_FILE).read_text())
        self.assertEqual(binding['target'], second)
        # The original evidence kept its original binding: it was never re-bound.
        evidence = json.loads((self.evidence_dir / (REF + '.json')).read_text())
        self.assertEqual(evidence['target'], first)
        # And the install manifest is untouched: nothing was reinstalled.
        self.assertEqual(manifest.read_bytes(), manifest_before)
        history = json.loads((self.state / learned_onboarding.ACTIVATION_HISTORY_FILE).read_text())
        self.assertEqual(history['bindings'][-1]['target'], second)
        self.assertEqual(history['bindings'][-1]['previous_target'], first)

    def test_same_file_plan_build_with_changed_launch_parameters_is_reusable(self):
        first = self.live_target()
        self.write_evidence(first)
        original = self.struct(first)
        original['compatibility'] = dict(BUILD, launch='profile-a')
        evidence = validate(self.recipe(), self.evidence_dir, target=first)['evidence']
        matcher.remember_verified(original, self.recipe(), evidence, source='goose')

        second = self.live_target()
        changed = self.struct(second)
        changed['compatibility'] = dict(BUILD, launch='profile-b')
        match = matcher.classify(changed)
        self.assertEqual(match['status'], 'exact', match)
        self.assertIn('launch parameters changed', match['reason'])
        plan = onboarding.plan_from_match(changed, match)
        self.assertEqual(plan['action'], 'install_reused_recipe')
        self.assertEqual(plan['observed_compatibility']['launch'], 'profile-a')

    def test_rebind_is_refused_without_the_rebind_scope(self):
        first = self.live_target()
        self.write_evidence(first)
        self.install_once(first)
        self.register_fingerprint(first)
        self.processes[0].terminate()
        self.processes[0].wait(timeout=10)
        second = self.live_target()

        with patch.dict(os.environ, {'ASG_ONBOARDING_REBIND': '0'}):
            result = onboarding.execute_install(
                self.reuse_plan(second), second,
                {'approved': True, 'scope': 'project', 'workspace': str(self.workspace)})

        self.assertEqual(result['status'], 'rebind_required', result)
        binding = json.loads((self.state / learned_onboarding.OBSERVATION_FILE).read_text())
        self.assertEqual(binding['target'], first)

    def test_missing_original_evidence_is_reported_not_raised(self):
        target = self.live_target()
        plan = {'adapter': 'file_plan', 'action': 'install_reused_recipe',
                'status': 'plan_pending_authorization',
                'workspace': str(self.workspace), 'reuse_recipe': self.recipe(),
                'reuse_evidence_refs': []}
        result = onboarding.execute_install(
            plan, target, {'approved': True, 'scope': 'project', 'workspace': str(self.workspace)})
        self.assertEqual(result['status'], 'reuse_requires_validation')
        self.assertEqual(result['missing'], 'original_evidence_missing')

    def test_missing_original_record_is_reported_not_raised(self):
        target = self.live_target()
        plan = {'adapter': 'file_plan', 'action': 'install_reused_recipe',
                'status': 'plan_pending_authorization',
                'workspace': str(self.workspace), 'reuse_recipe': self.recipe(),
                'reuse_evidence_refs': [REF], 'observed_compatibility': dict(BUILD)}
        result = onboarding.execute_install(
            plan, target, {'approved': True, 'scope': 'project', 'workspace': str(self.workspace)})
        self.assertEqual(result['status'], 'reuse_requires_validation')
        self.assertEqual(result['missing'], 'original_install_record_or_evidence')

    def test_reuse_still_requires_authorization(self):
        target = self.live_target()
        plan = {'adapter': 'file_plan', 'action': 'install_reused_recipe',
                'status': 'plan_pending_authorization',
                'workspace': str(self.workspace), 'reuse_recipe': self.recipe(),
                'reuse_evidence_refs': [REF]}
        result = onboarding.execute_install(plan, target, {'approved': False})
        self.assertEqual(result['status'], 'pending_authorization')


if __name__ == '__main__':
    unittest.main()
