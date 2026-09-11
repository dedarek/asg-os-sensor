# -*- coding: utf-8 -*-
"""learned_onboarding must consume a candidate observation_source declaration.

A successful install publishes a bound source config next to the install
record; a failed install and an undeclared candidate must publish nothing, and
the instance binding always comes from the executor, never the candidate.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime import learned_onboarding as onboarding
from runtime import observation_source as src


def _recipe(declaration=None, path="logs/probe.jsonl", files=None):
    hook = {"method": "file_plan", "workspace": None, "restart_required": "unknown",
            "capabilities": [], "verification": "v", "rollback": "r", "limitations": []}
    recipe = {
        "agent_identity_name": "fixture-agent",
        "match_features": {"runtime": "fixture"},
        "observation": "fixture",
        "fallback": "fixture",
        "hook": hook,
        "evidence_refs": ["ev-1-0123456789"],
        "install_plan": {"version": 1, "files": files or [
            {"path": "hook-probe.js", "content": "export default () => ({});\n",
             "expected_sha256": None}]},
    }
    if declaration is not None:
        recipe["observation_source"] = declaration
    return recipe


DECLARATION = {"log_path": "logs/probe.jsonl",
               "fields": {"event": "kind", "pid": "process", "timestamp": "at"},
               "event_names": {"hook.loaded": "probe.ready"}}


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "ws"
        (self.workspace / "logs").mkdir(parents=True)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.target = {"pid": 4242, "create_time": 1789000000.5}

    def tearDown(self):
        self._tmp.cleanup()

    def prepare(self, recipe):
        recipe = dict(recipe)
        recipe["hook"] = dict(recipe["hook"], workspace=str(self.workspace))
        with patch.object(onboarding, "validate",
                          return_value={"evidence": [], "hook_evidence_supported": False}):
            return onboarding.prepare(recipe, self.evidence, self.target, self.workspace)

    def test_declaration_is_validated_and_bound_by_the_executor(self):
        prepared = self.prepare(_recipe(DECLARATION))
        observation = prepared["observation"]

        self.assertEqual(observation["status"], "configured")
        self.assertEqual(observation["config"]["target"], self.target)
        self.assertEqual(observation["config"]["mapping_mode"], "explicit")
        self.assertEqual(observation["config"]["fields"]["event"], "kind")
        # prepare() resolves the supervisor workspace (macOS /var -> /private/var),
        # so the bound log path is the resolved one, still inside the workspace.
        log_path = Path(observation["config"]["log_path"])
        self.assertTrue(log_path.is_relative_to(self.workspace.resolve()))
        self.assertEqual(log_path.name, "probe.jsonl")

    def test_absent_declaration_is_not_configured_without_a_product_default(self):
        prepared = self.prepare(_recipe(None))
        self.assertEqual(prepared["observation"]["status"], "not_configured")
        self.assertNotIn("config", prepared["observation"])

    def test_candidate_cannot_bind_its_own_instance(self):
        bad = dict(DECLARATION, target={"pid": 1, "create_time": 1.0})
        with self.assertRaises(ValueError) as ctx:
            self.prepare(_recipe(bad))
        self.assertIn("executor binds", str(ctx.exception))

    def test_escaping_log_path_is_refused_at_prepare(self):
        with self.assertRaises(ValueError):
            self.prepare(_recipe(dict(DECLARATION, log_path="../escape.jsonl")))


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "ws"
        (self.workspace / "logs").mkdir(parents=True)
        self.state = self.root / "state"
        self.state.mkdir()
        self.target = {"pid": 4242, "create_time": 1789000000.5}

    def tearDown(self):
        self._tmp.cleanup()

    def prepared(self, declaration):
        recipe = _recipe(declaration)
        recipe["hook"] = dict(recipe["hook"], workspace=str(self.workspace))
        return {
            "status": "candidate_prepared", "workspace": str(self.workspace),
            "target": self.target, "candidate_digest": "d" * 64,
            "plan": recipe["install_plan"], "plan_digest": "p" * 64,
            "observation": ({"status": "configured",
                             "config": src.candidate_to_config(declaration, self.workspace, self.target)}
                            if declaration is not None else {"status": "not_configured", "reason": "x"}),
        }

    def run_execute(self, prepared, install_result):
        with patch.object(onboarding.learned_install, "install", return_value=install_result), \
             patch.object(onboarding.psutil, "Process") as proc:
            proc.return_value.create_time.return_value = self.target["create_time"]
            return onboarding.execute(prepared, self.state, approved_workspace=self.workspace,
                                      approved_candidate_digest="d" * 64)

    def test_successful_install_writes_a_reloadable_config(self):
        result = self.run_execute(self.prepared(DECLARATION), {"status": "installed"})

        observation = result["observation"]
        self.assertEqual(observation["status"], "configured")
        config_path = Path(observation["config_path"])
        self.assertTrue(config_path.is_file())
        # The config lives with the install record, never inside the target files.
        self.assertTrue(config_path.is_relative_to(self.state))
        self.assertFalse(config_path.is_relative_to(self.workspace))

        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["target"], self.target)
        self.assertEqual(saved["mapping_mode"], "explicit")

        # Round-trip: reading it back must not resurrect an undeclared role.
        loaded = src.load_config(config_path)
        self.assertNotIn("tool", loaded["fields"])
        self.assertNotIn("call_id", loaded["fields"])
        self.assertEqual(loaded["fields"], saved["fields"])
        self.assertEqual(loaded["event_names"], saved["event_names"])

    def test_failed_install_generates_no_config(self):
        with self.assertRaises(ValueError) as ctx:
            self.run_execute(self.prepared(DECLARATION), {"status": "failed"})
        self.assertIn("did not complete", str(ctx.exception))
        self.assertFalse((self.state / onboarding.OBSERVATION_FILE).exists())

    def test_installer_exception_generates_no_config(self):
        with patch.object(onboarding.learned_install, "install",
                          side_effect=ValueError("precondition changed")), \
             patch.object(onboarding.psutil, "Process") as proc:
            proc.return_value.create_time.return_value = self.target["create_time"]
            with self.assertRaises(ValueError):
                onboarding.execute(self.prepared(DECLARATION), self.state,
                                   approved_workspace=self.workspace,
                                   approved_candidate_digest="d" * 64)
        self.assertFalse((self.state / onboarding.OBSERVATION_FILE).exists())

    def test_undeclared_candidate_installs_without_a_config(self):
        result = self.run_execute(self.prepared(None), {"status": "installed"})
        self.assertEqual(result["observation"]["status"], "not_configured")
        self.assertIsNone(result["observation"]["config_path"])
        self.assertFalse((self.state / onboarding.OBSERVATION_FILE).exists())

    def test_already_installed_still_publishes_the_binding(self):
        result = self.run_execute(self.prepared(DECLARATION), {"status": "already_installed"})
        self.assertEqual(result["observation"]["status"], "configured")
        self.assertTrue(Path(result["observation"]["config_path"]).is_file())

    def test_approval_mismatch_writes_nothing(self):
        with patch.object(onboarding.psutil, "Process"):
            with self.assertRaises(PermissionError):
                onboarding.execute(self.prepared(DECLARATION), self.state,
                                   approved_workspace=self.workspace,
                                   approved_candidate_digest="wrong")
        self.assertFalse((self.state / onboarding.OBSERVATION_FILE).exists())


if __name__ == "__main__":
    unittest.main()
