# -*- coding: utf-8 -*-
"""Activation rebind after a restart: same workspace, same build, evidence required.

The function must never move a binding on a bare pid, never reinstall, and must
keep the pre-restart target record while recording the migration.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime import learned_onboarding as onboarding
from runtime import observation_source as src

WORKSPACE_BUILD = {'executable': 'exe-digest-abc', 'entry': 'entry-digest-def',
                   'platform': 'Darwin', 'architecture': 'arm64', 'runtime': 'node',
                   'entry_path': '/ws/app/index.js'}

DECLARATION = {'log_path': 'logs/probe.jsonl',
               'fields': {'event': 'kind', 'pid': 'process', 'timestamp': 'at'}}


class RebindTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / 'ws'
        (self.workspace / 'logs').mkdir(parents=True)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.old_target = {'pid': 1000, 'create_time': 1789000000.5}
        self.new_target = {'pid': 2000, 'create_time': 1789009000.25}
        # Seed the installer observation binding as execute() would leave it.
        config = src.candidate_to_config(DECLARATION, self.workspace, self.old_target)
        payload = {key: config[key] for key in
                   ('version', 'mapping_mode', 'target', 'log_path', 'fields', 'event_names')}
        (self.state / onboarding.OBSERVATION_FILE).write_text(
            json.dumps(payload, indent=2), encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def prepared(self, compatibility=None):
        return {'status': 'installed', 'workspace': str(self.workspace.resolve()),
                'target': dict(self.old_target),
                'compatibility': WORKSPACE_BUILD if compatibility is None else compatibility,
                'plan_digest': 'p' * 64, 'manifest': str(self.state / 'p.json')}

    def rebind(self, prepared=None, *, target=None, observed=WORKSPACE_BUILD,
               evidence=('exe digest + entry digest + inherited cmd/cwd/env',),
               workspace=None):
        with patch.object(onboarding.psutil, 'Process') as proc:
            proc.return_value.create_time.return_value = (target or self.new_target)['create_time']
            return onboarding.rebind_activation(
                prepared or self.prepared(), self.state,
                approved_workspace=workspace or self.workspace,
                new_target=target or self.new_target,
                observed_compatibility=observed, evidence=list(evidence),
                reason='restart of the same build')

    def test_same_build_rebind_rewrites_binding_and_keeps_history(self):
        result = self.rebind()
        self.assertEqual(result['status'], 'activation_rebound')
        self.assertEqual(result['previous_target'], self.old_target)
        self.assertEqual(result['target'], self.new_target)

        reloaded = src.load_config(Path(result['config_path']))
        self.assertEqual(reloaded['target'], self.new_target)
        self.assertEqual(reloaded['fields'], {'event': 'kind', 'pid': 'process', 'timestamp': 'at'})
        self.assertEqual(reloaded['mapping_mode'], 'explicit')

        history = json.loads(Path(result['history_path']).read_text(encoding='utf-8'))
        kinds = [item['kind'] for item in history['bindings']]
        self.assertEqual(kinds, ['initial', 'activation_rebind'])
        # The pre-restart target record is preserved verbatim.
        self.assertEqual(history['bindings'][0]['target'], self.old_target)
        self.assertEqual(history['bindings'][0]['compatibility'], WORKSPACE_BUILD)
        self.assertEqual(history['bindings'][1]['previous_target'], self.old_target)
        self.assertEqual(history['bindings'][1]['evidence'],
                         ['exe digest + entry digest + inherited cmd/cwd/env'])

    def test_corrupt_history_does_not_change_binding(self):
        path = self.state / onboarding.OBSERVATION_FILE
        before = path.read_bytes()
        (self.state / onboarding.ACTIVATION_HISTORY_FILE).write_text('{broken')
        with self.assertRaises(ValueError):
            self.rebind()
        self.assertEqual(path.read_bytes(), before)

    def test_second_rebind_records_current_predecessor(self):
        self.rebind()
        third = {'pid': 3000, 'create_time': 1789010000.5}
        result = self.rebind(target=third)
        self.assertEqual(result['previous_target'], self.new_target)

    def test_rebind_never_reinstalls_or_reapproves_a_candidate(self):
        with patch.object(onboarding.learned_install, 'install') as install:
            self.rebind()
        install.assert_not_called()
        # The installer manifest is untouched.
        self.assertFalse((self.state / 'p.json').exists())

    def test_bare_pid_without_evidence_is_refused(self):
        # A caller handing over only a new pid has no build snapshot at all.
        with self.assertRaises(ValueError) as ctx:
            self.rebind(observed=None)
        self.assertIn('refusing activation rebind', str(ctx.exception))
        # And an empty evidence list is a bare pid even with a matching build.
        with self.assertRaises(ValueError) as ctx:
            self.rebind(evidence=[])
        self.assertIn('not a bare pid', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.rebind(evidence=[''])
        self.assertIn('not a bare pid', str(ctx.exception))
        # Nothing was rewritten.
        self.assertEqual(src.load_config(self.state / onboarding.OBSERVATION_FILE)['target'],
                         self.old_target)

    def test_different_build_is_not_a_restart(self):
        upgraded = dict(WORKSPACE_BUILD, executable='exe-digest-OTHER')
        with self.assertRaises(ValueError) as ctx:
            self.rebind(observed=upgraded)
        self.assertIn('构建身份不一致', str(ctx.exception))
        self.assertFalse((self.state / onboarding.ACTIVATION_HISTORY_FILE).exists())

    def test_missing_recorded_build_compatibility_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.rebind(self.prepared(compatibility=None) | {'compatibility': None})
        self.assertIn('no recorded build compatibility', str(ctx.exception))

    def test_other_workspace_is_refused(self):
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaises(PermissionError):
            self.rebind(workspace=other)

    def test_dead_new_target_is_refused(self):
        with patch.object(onboarding.psutil, 'Process') as proc:
            proc.return_value.create_time.return_value = 123.0
            with self.assertRaises(ValueError) as ctx:
                onboarding.rebind_activation(
                    self.prepared(), self.state, approved_workspace=self.workspace,
                    new_target=self.new_target, observed_compatibility=WORKSPACE_BUILD,
                    evidence=['e'])
        self.assertIn('changed before rebinding', str(ctx.exception))

    def test_rebinding_to_the_same_target_is_a_noop(self):
        result = self.rebind(target=self.old_target)
        self.assertEqual(result['status'], 'already_bound')
        self.assertFalse((self.state / onboarding.ACTIVATION_HISTORY_FILE).exists())

    def test_missing_binding_is_an_explicit_error(self):
        (self.state / onboarding.OBSERVATION_FILE).unlink()
        with self.assertRaises(ValueError) as ctx:
            self.rebind()
        self.assertIn('no observation binding to rebind', str(ctx.exception))
class PrepareExecuteIntegrationTests(unittest.TestCase):
    """prepare() must record the build so execute() output can be rebound."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / 'ws'
        (self.workspace / 'logs').mkdir(parents=True)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        self.old_target = {'pid': 1000, 'create_time': 1789000000.5}
        self.new_target = {'pid': 2000, 'create_time': 1789009000.25}

    def tearDown(self):
        self._tmp.cleanup()

    def test_prepared_record_carries_build_and_execute_result_can_rebind(self):
        recipe = {'agent_identity_name': 'fixture', 'match_features': {'runtime': 'node'},
                  'observation': 'fixture', 'fallback': 'fixture',
                  "hook": {"method": "file_plan", "workspace": str(self.workspace),
                           "restart_required": "unknown", "capabilities": [],
                           "verification": "v", "rollback": "r", "limitations": []},
                  "evidence_refs": ["ev-1-0123456789"], "observation_source": DECLARATION,
                  "install_plan": {"version": 1, "files": [
                      {"path": "hook.js", "content": "export default () => ({});\n",
                       "expected_sha256": None}]}}
        with patch.object(onboarding, 'validate',
                          return_value={'evidence': [], 'hook_evidence_supported': False}):
            prepared = onboarding.prepare(recipe, self.evidence, self.old_target,
                                          self.workspace, compatibility=WORKSPACE_BUILD)
        self.assertEqual(prepared['compatibility'], WORKSPACE_BUILD)
        self.assertEqual(prepared['observation']['config']['target'], self.old_target)

        with patch.object(onboarding.learned_install, 'install', return_value={'status': 'installed'}), \
             patch.object(onboarding.psutil, 'Process') as proc:
            proc.return_value.create_time.return_value = self.old_target['create_time']
            executed = onboarding.execute(prepared, self.state,
                                          approved_workspace=self.workspace,
                                          approved_candidate_digest=prepared['candidate_digest'])
        self.assertEqual(executed['compatibility'], WORKSPACE_BUILD)
        self.assertEqual(executed['workspace'], prepared['workspace'])
        self.assertEqual(executed['observation']['status'], 'configured')

        # The executed record is directly rebindable to the restarted instance.
        with patch.object(onboarding.psutil, 'Process') as proc:
            proc.return_value.create_time.return_value = self.new_target['create_time']
            rebound = onboarding.rebind_activation(
                executed, self.state, approved_workspace=self.workspace,
                new_target=self.new_target, observed_compatibility=WORKSPACE_BUILD,
                evidence=['verified inherited cmd/cwd/env and exe/build'])
        self.assertEqual(rebound['status'], 'activation_rebound')
        self.assertEqual(src.load_config(Path(rebound['config_path']))['target'], self.new_target)


if __name__ == '__main__':
    unittest.main()
