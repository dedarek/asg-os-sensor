import copy
import unittest

from runtime import recipe_bundle

DB = {"fingerprints": [{"id": "fp-1", "name": "Example Agent", "revision": 2,
                       "features": {"exe": "/opt/example/bin/agent"},
                       "hook_recipe": {"agent_identity_name": "Example Agent",
                                       "hook": {"method": "file_plan"}},
                       "investigation_verified": True, "hook_verified": False,
                       "recipe_source": "goose", "revisions": [
                           {"revision": 2, "compatibility": {"executable": "abc", "platform": "Darwin"}}]}]}
BUILD = {"executable": "abc", "platform": "Darwin"}


class BundleTests(unittest.TestCase):
    def test_export_carries_recipe_constraints_and_verification(self):
        bundle = recipe_bundle.export_bundle("fp-1", db=DB)
        self.assertEqual(bundle["schema"], recipe_bundle.SCHEMA)
        self.assertEqual(bundle["recipe"]["hook"]["method"], "file_plan")
        self.assertEqual(bundle["constraints"]["compatibility"], BUILD)
        self.assertFalse(bundle["verification"]["hook_verified"])
        self.assertEqual(bundle["integrity"]["digest"], recipe_bundle.digest(bundle))

    def test_export_refuses_entry_without_recipe(self):
        db = {"fingerprints": [{"id": "fp-2", "name": "x", "hook_recipe": None}]}
        with self.assertRaises(ValueError):
            recipe_bundle.export_bundle("fp-2", db=db)
        with self.assertRaises(LookupError):
            recipe_bundle.export_bundle("missing", db=DB)

    def test_validation_detects_tampering(self):
        bundle = recipe_bundle.export_bundle("fp-1", db=DB)
        self.assertTrue(recipe_bundle.validate_bundle(bundle)["ok"])
        tampered = copy.deepcopy(bundle)
        tampered["recipe"]["hook"]["method"] = "something_else"
        with self.assertRaises(ValueError):
            recipe_bundle.validate_bundle(tampered)

    def test_exact_build_is_ready_but_still_not_proven(self):
        bundle = recipe_bundle.export_bundle("fp-1", db=DB)
        result = recipe_bundle.import_bundle(bundle, observed_build=dict(BUILD), workspace="/tmp/ws")
        self.assertEqual(result["status"], "ready_for_authorization")
        self.assertIn("已安装", result["plan"]["not_proven_by_import"])
        self.assertIn("该实例独立验收", result["plan"]["requires"])

    def test_changed_build_needs_review(self):
        bundle = recipe_bundle.export_bundle("fp-1", db=DB)
        changed = {"executable": "zzz", "platform": "Darwin"}
        result = recipe_bundle.import_bundle(bundle, observed_build=changed)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["compatibility"]["differences"], ["executable"])

    def test_missing_observation_is_unknown_not_compatible(self):
        bundle = recipe_bundle.export_bundle("fp-1", db=DB)
        result = recipe_bundle.import_bundle(bundle, observed_build=None)
        self.assertEqual(result["compatibility"]["status"], "unknown")
        self.assertEqual(result["status"], "needs_review")

    def test_verified_flag_warning_is_surfaced(self):
        bundle = recipe_bundle.export_bundle("fp-1", db=DB)
        self.assertIn("exporting side never verified the Hook effect",
                      recipe_bundle.validate_bundle(bundle)["warnings"])


if __name__ == "__main__":
    unittest.main()
