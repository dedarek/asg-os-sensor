"""Trial builds must evolve the supervisor-matched fingerprint family.

The supervisor knows the matched family id before spawning Goose, so it must
inject that id (ASG_TRIAL_FAMILY_ID), expose it in get_control_contract, and
reject a proposal that ignores it. Previously the id was only buried in a
600KB get_prior_recipe payload and the whole investigation was discarded at
commit time after Goose failed to find it.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from runtime import analyst_tools as at


def _recipe(evolves="__omit__"):
    features = {"runtime": "node"}
    if evolves != "__omit__":
        features["evolves_prior_harness"] = evolves
    return {
        "agent_identity_name": "fixture-agent",
        "match_features": features,
        "observation": "fixture",
        "fallback": "fixture",
        "confidence": 0.7,
        "hook": {"method": "unsupported", "restart_required": "unknown",
                 "capabilities": ["observe"], "verification": "f", "rollback": "f",
                 "limitations": ["f"]},
        "evidence_refs": ["ev-1-0123456789"],
    }


class ControlContractTrialFamilyTests(unittest.TestCase):
    def _contract(self, env):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"ASG_RUN_DIR": tmp, **env}, clear=False), \
             patch.object(at, "target_process", return_value=psutil.Process()):
            return at.call_tool("get_control_contract", {})

    def test_trial_family_surface_when_env_is_injected(self):
        result = self._contract({"ASG_TRIAL_FAMILY_ID": "harness-7f3a19"})
        self.assertEqual(result["trial_family"]["id"], "harness-7f3a19")
        self.assertIn("evolves_prior_harness", result["trial_family"]["required_action"])

    def test_no_trial_family_key_without_env(self):
        os.environ.pop("ASG_TRIAL_FAMILY_ID", None)
        result = self._contract({})
        self.assertNotIn("trial_family", result)


class ProposeTrialGateTests(unittest.TestCase):
    def _propose(self, recipe, trial_env):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"ASG_TRIAL_FAMILY_ID": trial_env} if trial_env else {},
                clear=False), \
             patch.object(at, "target_process", return_value=psutil.Process()), \
             patch.object(at, "_validate_investigation_summary"), \
             patch("runtime.recipe_validation.validate"), \
             patch.object(at, "RECIPE_DIR", Path(tmp)), \
             patch.object(at, "EVIDENCE_DIR", Path(tmp)):
            if not trial_env:
                os.environ.pop("ASG_TRIAL_FAMILY_ID", None)
            result = at.call_tool("propose_recipe", {"recipe": recipe})
            saved_path = Path(result["candidate_path"])
            if saved_path.exists():
                result["saved"] = json.loads(saved_path.read_text(encoding="utf-8"))
            return result

    def test_missing_evolution_id_is_rejected_with_the_family_id(self):
        with self.assertRaises(ValueError) as ctx:
            self._propose(_recipe(), "harness-7f3a19")
        message = str(ctx.exception)
        self.assertIn("evolves_prior_harness", message)
        self.assertIn("harness-7f3a19", message)

    def test_wrong_evolution_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self._propose(_recipe(evolves="harness-01"), "harness-7f3a19")

    def test_matching_evolution_id_is_accepted(self):
        result = self._propose(_recipe(evolves="harness-7f3a19"), "harness-7f3a19")
        self.assertEqual(
            result["saved"]["recipe"]["match_features"]["evolves_prior_harness"], "harness-7f3a19")

    def test_new_family_path_is_untouched_without_trial_env(self):
        result = self._propose(_recipe(), None)
        self.assertIn("saved", result)


if __name__ == "__main__":
    unittest.main()
