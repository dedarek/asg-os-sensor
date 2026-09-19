# -*- coding: utf-8 -*-
"""Failing regressions for three coordinate() acceptance gates.

These are NOT real Agent Hook acceptance tests. They use a disposable workspace
and real short-lived local processes, with explicitly synthetic investigation
evidence whose only purpose is to carry a target binding.

Nothing here mocks validate(), the build comparison or process liveness: the
gates must be exercised as they really run. Each test states the behaviour the
coordination entry must have once the recorded install baseline is used.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import psutil

from runtime import learned_onboarding as onboarding
from runtime import observation_source as src

REF = 'ev-1-0123456789'
DECLARATION = {'log_path': 'logs/probe.jsonl',
               'fields': {'event': 'kind', 'pid': 'process', 'timestamp': 'at'}}


def _install_build(entry_digest='entry-a'):
    return {'executable': 'exe-a', 'entry': entry_digest, 'platform': 'Darwin',
            'architecture': 'arm64', 'runtime': 'python', 'entry_path': '/ws/worker.py'}


def _changed_build():
    # Same family (same entry_path basename) but different bytes: an upgrade,
    # never a restart of the same build.
    return {'executable': 'exe-b', 'entry': 'entry-b', 'platform': 'Darwin',
            'architecture': 'arm64', 'runtime': 'python', 'entry_path': '/ws/worker.py'}


class CoordinateGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / 'ws'
        (self.workspace / 'logs').mkdir(parents=True)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.evidence_dir = self.root / 'evidence'
        self.evidence_dir.mkdir()
        self.processes = []

    def tearDown(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self._tmp.cleanup()

    def live_target(self):
        """Start a real throwaway process and return its real pid/create_time."""
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.processes.append(proc)
        return {'pid': proc.pid, 'create_time': psutil.Process(proc.pid).create_time()}

    def write_evidence(self, target):
        """Synthetic investigation evidence explicitly bound to one instance."""
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

    def scope(self, *, install=True, rebind=False):
        return onboarding.authorization_scope(approved_workspace=self.workspace,
                                              allow_install=install, allow_rebind=rebind)

    def coordinate(self, *, target, build, evidence=None, rebind=False,
                   install=True):
        return onboarding.coordinate(self.recipe(), self.evidence_dir, target,
                                     workspace=self.workspace, state_dir=self.state,
                                     authorization=self.scope(install=install, rebind=rebind),
                                     observed_compatibility=build, evidence=evidence,
                                     reason='test')

    def binding(self):
        return src.load_config(self.state / onboarding.OBSERVATION_FILE)['target']

    def test_investigation_target_can_differ_from_activation_target(self):
        """Gate 2: the old investigation evidence must not be re-checked against
        the new activation instance, which a real restart never matches."""
        investigation = self.live_target()
        self.write_evidence(investigation)
        self.coordinate(target=investigation, build=_install_build())
        self.processes[0].terminate()
        self.processes[0].wait(timeout=10)  # the investigated instance has exited
        activation = self.live_target()
        self.write_evidence(investigation)

        result = self.coordinate(target=activation, build=_install_build(),
                                 rebind=True, evidence=['synthetic: same recorded build verified'])

        self.assertEqual(result['status'], 'rebound')
        self.assertEqual(self.binding(), activation)
        self.assertEqual(result['target'], activation)

    def test_install_time_build_baseline_is_not_lost_on_a_later_call(self):
        """Gate 1: coordinate must compare the observed build against the build
        recorded at install time, not against a value it just overwrote."""
        investigation = self.live_target()
        self.write_evidence(investigation)
        installed = self.coordinate(target=investigation, build=_install_build())
        self.assertEqual(installed['status'], 'installed')

        restarted = self.live_target()
        # Same plan, same family, different bytes: this is an upgrade, not a
        # restart of the installed build, so it must not be silently rebound.
        outcome = None
        try:
            outcome = self.coordinate(target=restarted, build=_changed_build(),
                                      evidence=['test: build changed'], rebind=True)
        except ValueError as exc:
            message = str(exc)
            self.assertNotIn('different instance than the investigation target', message)
            self.assertTrue('构建' in message or 'build' in message.lower(), message)
        else:
            self.assertNotEqual(outcome['status'], 'rebound')
        # Whatever the outcome, the binding must not have moved to the new build.
        self.assertEqual(self.binding(), investigation)

    def test_install_manifest_truth_is_read_not_assumed_from_existence(self):
        """Gate 3: manifest presence alone must not mean installed."""
        target = self.live_target()
        self.write_evidence(target)
        installed = self.coordinate(target=target, build=_install_build())
        self.assertEqual(installed['status'], 'installed')
        manifest = self.state / (installed['plan_digest'] + '.json')
        self.assertTrue(manifest.is_file())

        with self.subTest(case='rolled_back'):
            data = json.loads(manifest.read_text(encoding='utf-8'))
            data['status'] = 'rolled_back'
            manifest.write_text(json.dumps(data), encoding='utf-8')
            before = (self.state / onboarding.OBSERVATION_FILE).read_bytes()
            with self.assertRaisesRegex(ValueError, 'manifest|files changed|precondition changed'):
                self.coordinate(target=target, build=_install_build())
            self.assertEqual((self.state / onboarding.OBSERVATION_FILE).read_bytes(), before)

        with self.subTest(case='hook_file_modified'):
            data['status'] = 'installed'
            manifest.write_text(json.dumps(data), encoding='utf-8')
            (self.workspace / 'hook-probe.py').write_text('print(2)\n', encoding='utf-8')
            before = (self.state / onboarding.OBSERVATION_FILE).read_bytes()
            with self.assertRaisesRegex(ValueError, 'manifest|files changed'):
                self.coordinate(target=target, build=_install_build())
            self.assertEqual((self.state / onboarding.OBSERVATION_FILE).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
