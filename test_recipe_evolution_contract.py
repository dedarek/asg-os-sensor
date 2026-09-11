"""Contract tests for match_features.evolves_prior_harness.

A real Goose candidate filled this id selector with a whole sentence, and the
matcher then treated that truthy string as a harness id. These tests pin the
fix: the field is an id or nothing, the value is never stripped, and a bad
value reaches the model as an actionable error inside propose_recipe.
"""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime import analyst_tools as at
from runtime.recipe_validation import validate, validate_evolution_target

SENTENCE = ("none matched: prior_recipe is empty and prior_experience matched_by=none for this instance; "
            "the mechanism below comes from the shipped binary's loader code")


def _recipe(**features):
    hook = {"method": "unsupported", "restart_required": "unknown", "capabilities": ["observe"],
            "verification": "fixture", "rollback": "fixture", "limitations": ["fixture"]}
    return {"agent_identity_name": "fixture-agent", "match_features": dict(features),
            "observation": "fixture", "fallback": "fixture", "hook": hook,
            "evidence_refs": ["ev-1-0123456789"]}


class EvolutionFieldContractTests(unittest.TestCase):
    def test_absent_null_and_empty_mean_no_evolution(self):
        for features in ({}, {"evolves_prior_harness": None}, {"evolves_prior_harness": ""}):
            with self.subTest(features=features):
                self.assertIsNone(validate_evolution_target(_recipe(**features), {"harness-01"}))

    def test_format_only_check_when_no_prior_ids_are_supplied(self):
        recipe = _recipe(evolves_prior_harness="harness-01")
        self.assertIsNone(validate_evolution_target(recipe, None))

    def test_exact_known_id_passes_and_recipe_is_not_modified(self):
        recipe = _recipe(evolves_prior_harness="harness-7f3a19")
        before = json.dumps(recipe, sort_keys=True)
        self.assertIsNone(validate_evolution_target(recipe, {"harness-7f3a19", "harness-01"}))
        self.assertIn("evolves_prior_harness", recipe["match_features"])
        self.assertEqual(json.dumps(recipe, sort_keys=True), before)

    def test_natural_language_is_rejected_with_fixable_message(self):
        with self.assertRaises(ValueError) as ctx:
            validate_evolution_target(_recipe(evolves_prior_harness=SENTENCE), None)
        message = str(ctx.exception)
        self.assertIn("match_features.evolves_prior_harness", message)
        self.assertIn("harness-01", message)
        self.assertIn("observation/fallback", message)
        self.assertIn("Received:", message)
        self.assertLessEqual(len(message), 400)

    def test_near_miss_ids_are_rejected_too(self):
        for value in ("harness-01 and harness-02", "the harness-01 family",
                      "none matched", "harness_01", " harness-01", "harness-01 ", "   "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_evolution_target(_recipe(evolves_prior_harness=value), None)

    def test_non_string_is_rejected(self):
        for value in (["harness-01"], {"id": "harness-01"}, 7, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_evolution_target(_recipe(evolves_prior_harness=value), None)

    def test_well_formed_but_unknown_id_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_evolution_target(_recipe(evolves_prior_harness="harness-99"), {"harness-01"})
        message = str(ctx.exception)
        self.assertIn("harness-99", message)
        self.assertIn("known ids", message)
        self.assertIn("harness-01", message)

    def test_unknown_id_reports_none_when_prior_is_empty(self):
        with self.assertRaises(ValueError) as ctx:
            validate_evolution_target(_recipe(evolves_prior_harness="harness-01"), set())
        self.assertIn("known ids: none", str(ctx.exception))


class EvolutionGateOrderingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.evidence_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_evidence(self, target):
        (self.evidence_dir / "ev-1-0123456789.json").write_text(json.dumps({
            "evidence_id": "ev-1-0123456789", "tool": "get_target_context", "target": target,
            "result": {"target": target, "local_evidence": {"runtime": "fixture"}},
        }), encoding="utf-8")

    def test_validate_rejects_field_before_reading_evidence(self):
        recipe = _recipe(evolves_prior_harness=SENTENCE)
        with self.assertRaises(ValueError) as ctx:
            validate(recipe, self.evidence_dir, known_harness_ids={"harness-01"})
        message = str(ctx.exception)
        self.assertIn("evolves_prior_harness", message)
        self.assertNotIn("evidence reference", message)

    def test_field_absent_does_not_skip_the_existing_recipe_field_gate(self):
        broken = _recipe(runtime="fixture")
        del broken["hook"]
        with self.assertRaises(ValueError) as ctx:
            validate(broken, self.evidence_dir)
        self.assertIn("Missing or invalid recipe field", str(ctx.exception))
        self.assertNotIn("evolves_prior_harness", str(ctx.exception))

    def test_field_absent_still_enforces_evidence_instance_binding(self):
        self._write_evidence({"pid": 1, "create_time": 1.0})
        with self.assertRaises(ValueError) as ctx:
            validate(_recipe(runtime="fixture"), self.evidence_dir, target={"pid": 2, "create_time": 2.0})
        self.assertIn("different instance", str(ctx.exception))


class RealCandidateShapeTests(unittest.TestCase):
    """The exact shape from the real candidate that triggered the matcher bug."""

    REAL_FIELD_VALUE = (
        "none matched: prior_recipe is empty and prior_experience matched_by=none for this instance; "
        "the mechanism below comes from the shipped binary's loader/dispatch code, not from the "
        "pre-existing hand-authored adapter found on disk")

    def test_real_sentence_is_no_longer_a_valid_selector(self):
        recipe = {"match_features": {"runtime": "Bun-compiled standalone opencode.exe",
                                     "evolves_prior_harness": self.REAL_FIELD_VALUE}}
        with self.assertRaises(ValueError) as ctx:
            validate_evolution_target(recipe, {"harness-01", "harness-02"})
        message = str(ctx.exception)
        self.assertIn("exactly one prior harness id", message)
        self.assertIn("null", message)
        # The message must be short enough to read inside a tool result.
        self.assertLess(len(message), 420)

    def test_truthy_sentence_would_never_be_treated_as_an_id(self):
        # Guards the original failure mode: any non-id truthy value is refused,
        # so a downstream truthiness check can no longer masquerade as an id.
        from runtime.recipe_validation import HARNESS_ID_RE
        self.assertIsNone(HARNESS_ID_RE.fullmatch(self.REAL_FIELD_VALUE))
        self.assertTrue(self.REAL_FIELD_VALUE)  # it was truthy, which is why it slipped through


class ProposeRecipeFeedbackTests(unittest.TestCase):
    """The model must be able to fix this inside the same tool interaction."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.recipe_dir = self.root / "recipes"
        self.evidence_dir = self.root / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _prior_db(self, ids):
        path = self.root / "fingerprints.json"
        path.write_text(json.dumps({"fingerprints": [{"id": item} for item in ids]}), encoding="utf-8")
        return path

    def _call(self, recipe, stub_gate=False):
        """Negative cases must run the real gate; only accept-paths stub it."""
        gate = (patch("runtime.recipe_validation.validate",
                      return_value={"evidence": [], "hook_evidence_supported": False})
                if stub_gate else contextlib.nullcontext())
        with patch.object(at, "target_process"), \
             patch.object(at, "_validate_investigation_summary"), \
             patch.object(at, "RECIPE_DIR", self.recipe_dir), \
             patch.object(at, "EVIDENCE_DIR", self.evidence_dir), \
             gate:
            return at.call_tool("propose_recipe", {"recipe": recipe})

    def test_sentence_is_refused_in_tool_and_writes_no_candidate(self):
        prior = self._prior_db(["harness-01"])
        with patch.dict(os.environ, {"ASG_FINGERPRINT_DB": str(prior)}):
            with self.assertRaises(ValueError) as ctx:
                self._call(_recipe(evolves_prior_harness=SENTENCE))
        self.assertIn("evolves_prior_harness", str(ctx.exception))
        self.assertFalse((self.recipe_dir / "candidate.json").exists())

    def test_unknown_id_is_refused_in_tool(self):
        prior = self._prior_db(["harness-01"])
        with patch.dict(os.environ, {"ASG_FINGERPRINT_DB": str(prior)}):
            with self.assertRaises(ValueError) as ctx:
                self._call(_recipe(evolves_prior_harness="harness-4242"))
        self.assertIn("harness-4242", str(ctx.exception))
        self.assertFalse((self.recipe_dir / "candidate.json").exists())

    def test_exact_prior_id_is_accepted_and_preserved(self):
        prior = self._prior_db(["harness-01"])
        with patch.dict(os.environ, {"ASG_FINGERPRINT_DB": str(prior)}):
            result = self._call(_recipe(evolves_prior_harness="harness-01"), stub_gate=True)
        stored = json.loads((self.recipe_dir / "candidate.json").read_text(encoding="utf-8"))
        self.assertEqual(result["recipe"]["match_features"]["evolves_prior_harness"], "harness-01")
        self.assertEqual(stored["recipe"]["match_features"]["evolves_prior_harness"], "harness-01")

    def test_absent_field_does_not_read_the_prior_at_all(self):
        # A corrupt prior must not matter when the recipe proposes a new family.
        broken = self.root / "fingerprints.json"
        broken.write_text("{not json", encoding="utf-8")
        with patch.dict(os.environ, {"ASG_FINGERPRINT_DB": str(broken)}):
            result = self._call(_recipe(runtime="fixture"), stub_gate=True)
        self.assertEqual(result["recipe"]["match_features"]["runtime"], "fixture")

    def test_declared_evolution_does_read_the_prior_and_reports_corruption(self):
        broken = self.root / "fingerprints.json"
        broken.write_text("{not json", encoding="utf-8")
        with patch.dict(os.environ, {"ASG_FINGERPRINT_DB": str(broken)}):
            with self.assertRaises(ValueError):
                self._call(_recipe(evolves_prior_harness="harness-01"))


if __name__ == "__main__":
    unittest.main()
