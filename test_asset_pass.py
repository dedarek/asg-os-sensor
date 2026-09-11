"""Offline truth tests for ``runtime.asset_pass.outcome``.

These tests never start Goose, never touch the network, and never install a
Hook. They pin the contract that a zero exit code is necessary but never
sufficient: the persisted findings and evidence provenance are rechecked
before an asset checkpoint may be reported as complete.
"""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path

from runtime import asset_pass, investigation_findings
from runtime.status import presentation

TARGET = {"pid": 4242, "create_time": 1789000000.5}
OTHER_TARGET = {"pid": 4242, "create_time": 1789000999.25}


class AssetPassOutcomeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / "run"
        self.run_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    @property
    def findings_path(self) -> Path:
        return self.run_dir / "investigation_findings.json"

    def write_evidence(self, ref: str, target: dict | None = None) -> None:
        path = self.run_dir / "evidence" / (ref + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"evidence_id": ref, "target": target or TARGET}), encoding="utf-8")

    def save(self, finding: dict, target: dict | None = None) -> None:
        investigation_findings.save_finding(self.findings_path, target or TARGET, finding)

    def identity(self, refs: list[str]) -> dict:
        return {"kind": "identity", "status": "identified",
                "value": {"name": "demo-runtime"}, "evidence_refs": refs}

    def asset(self, name: str, status: str, value, refs: list[str]) -> dict:
        return {"kind": "asset", "asset": name, "status": status,
                "value": value, "evidence_refs": refs}

    def save_identity(self, ref: str = "ev-1-aaaa") -> None:
        self.save(self.identity([ref]))
        self.write_evidence(ref)

    def test_nonzero_returncode_is_rejected(self):
        self.save_identity()
        self.save(self.asset("mcp", "collected", [{"name": "local"}], ["ev-2-bbbb"]))
        self.write_evidence("ev-2-bbbb")
        for code in (1, 7, -9, 137):
            self.assertIsNone(asset_pass.outcome(self.run_dir, TARGET, code))
        # A fully valid checkpoint is still refused when Goose did not exit cleanly.
        self.assertIsNotNone(asset_pass.outcome(self.run_dir, TARGET, 0))

    def test_missing_findings_is_rejected(self):
        self.assertIsNone(asset_pass.outcome(self.run_dir, TARGET, 0))

    def test_assets_without_identity_survive_as_partial(self):
        self.save(self.asset('model_gateway', 'collected', {'model': 'configured-model'}, ['ev-2-bbbb']))
        self.write_evidence('ev-2-bbbb')
        result = asset_pass.outcome(self.run_dir, TARGET, 0)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['asset_checkpoint']['collected'], ['model_gateway'])
        self.assertIsNone(result['partial_findings']['findings'].get('identity'))

    def test_unusable_findings_raise_instead_of_silently_passing(self):
        self.findings_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            asset_pass.outcome(self.run_dir, TARGET, 0)
        # An object without a matching target is an incompatible file, not a missing one.
        self.findings_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            asset_pass.outcome(self.run_dir, TARGET, 0)

    def test_findings_from_another_instance_are_rejected(self):
        self.save(self.identity(["ev-1-aaaa"]), target=OTHER_TARGET)
        self.write_evidence("ev-1-aaaa", target=OTHER_TARGET)
        with self.assertRaises(ValueError):
            asset_pass.outcome(self.run_dir, TARGET, 0)

    def test_evidence_from_another_instance_is_rejected(self):
        self.save_identity()
        self.save(self.asset("mcp", "empty", [], ["ev-2-bbbb"]))
        self.write_evidence("ev-2-bbbb", target=OTHER_TARGET)
        with self.assertRaises(ValueError) as ctx:
            asset_pass.outcome(self.run_dir, TARGET, 0)
        self.assertIn("another instance", str(ctx.exception))

    def test_invalid_evidence_reference_is_rejected(self):
        self.save_identity()
        self.save(self.asset("mcp", "empty", [], ["not-an-evidence"]))
        with self.assertRaises(ValueError) as ctx:
            asset_pass.outcome(self.run_dir, TARGET, 0)
        self.assertIn("Invalid asset evidence reference", str(ctx.exception))

    def test_unresolvable_evidence_reference_never_passes_silently(self):
        self.save(self.identity(["ev-9-zzzz"]))  # format-valid reference, no evidence file written
        with self.assertRaises(FileNotFoundError):
            asset_pass.outcome(self.run_dir, TARGET, 0)

    def test_four_reported_groups_with_real_collected_values(self):
        self.save_identity()
        self.save(self.asset("model_gateway", "collected", {"base_url": "http://127.0.0.1"}, ["ev-2-bbbb"]))
        self.write_evidence("ev-2-bbbb")
        self.save(self.asset("mcp", "collected", [{"name": "local"}], ["ev-3-cccc"]))
        self.write_evidence("ev-3-cccc")
        self.save(self.asset("skills", "empty", [], ["ev-4-dddd"]))
        self.write_evidence("ev-4-dddd")
        self.save(self.asset("rules", "unknown", None, ["ev-5-eeee"]))
        self.write_evidence("ev-5-eeee")

        result = asset_pass.outcome(self.run_dir, TARGET, 0)

        self.assertEqual(result["status"], "assets_collected")
        self.assertEqual(set(result["asset_checkpoint"]["reported"]),
                         set(investigation_findings.ASSET_NAMES))
        self.assertEqual(sorted(result["asset_checkpoint"]["collected"]),
                         ["mcp", "model_gateway"])
        self.assertIn("2 类有实际数据", result["message"])
        self.assertIn("4 类已报告", result["message"])
        self.assertEqual(result["partial_findings"]["target"], TARGET)

    def test_collected_but_empty_values_stay_partial(self):
        self.save_identity()
        for index, name in enumerate(investigation_findings.ASSET_NAMES, start=2):
            ref = f"ev-{index}-abcd"
            self.save(self.asset(name, "collected", [] if index % 2 else {}, [ref]))
            self.write_evidence(ref)

        result = asset_pass.outcome(self.run_dir, TARGET, 0)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["asset_checkpoint"]["collected"], [])
        self.assertEqual(set(result["asset_checkpoint"]["reported"]),
                         set(investigation_findings.ASSET_NAMES))

    def test_incomplete_groups_stay_partial(self):
        self.save_identity()
        self.save(self.asset("mcp", "collected", [{"name": "local"}], ["ev-2-bbbb"]))
        self.write_evidence("ev-2-bbbb")

        result = asset_pass.outcome(self.run_dir, TARGET, 0)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["asset_checkpoint"]["collected"], ["mcp"])

    def test_all_unknown_groups_are_partial_only(self):
        self.save_identity()
        for index, name in enumerate(investigation_findings.ASSET_NAMES, start=2):
            ref = f"ev-{index}-ffff"
            self.save(self.asset(name, "unknown", None, [ref]))
            self.write_evidence(ref)

        result = asset_pass.outcome(self.run_dir, TARGET, 0)

        self.assertEqual(result["status"], "partial")
        self.assertNotEqual(result["status"], "assets_collected")
        self.assertEqual(result["asset_checkpoint"]["collected"], [])
        self.assertEqual(set(result["asset_checkpoint"]["reported"]),
                         set(investigation_findings.ASSET_NAMES))

    def test_asset_checkpoint_never_claims_a_hook(self):
        self.save_identity()
        fixtures = [("model_gateway", "collected", {"base_url": "http://127.0.0.1"}),
                    ("mcp", "empty", []),
                    ("skills", "unknown", None),
                    ("rules", "unknown", None)]
        for index, (name, status, value) in enumerate(fixtures, start=2):
            ref = f"ev-{index}-bbbb"
            self.save(self.asset(name, status, value, [ref]))
            self.write_evidence(ref)

        result = asset_pass.outcome(self.run_dir, TARGET, 0)
        self.assertEqual(result["status"], "assets_collected")
        checkpoint = result["asset_checkpoint"]
        self.assertFalse(checkpoint["hook_proposed"])
        self.assertFalse(checkpoint["installed"])
        self.assertIn("未生成新 Hook 配方", result["message"])
        self.assertIn("未安装", result["message"])

        state = presentation(True, False, {"status": result["status"],
                                           "message": result["message"],
                                           "log_dir": str(self.run_dir)})
        self.assertEqual(state["investigation"]["status"], "assets_collected")
        self.assertIn("非 Hook 接入", state["investigation"]["label"])
        self.assertEqual(state["hook_state"]["status"], "not_installed")
        self.assertFalse(state["hook_state"]["verified"])
        self.assertTrue(all(item["status"] == "not_collected" for item in state["assets"].values()))


if __name__ == "__main__":
    unittest.main()
