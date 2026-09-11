# -*- coding: utf-8 -*-
"""Candidate observation_source declaration -> source config contract.

The candidate may describe how its own Hook writes a log, but never which
instance it is bound to. Two arbitrarily named log schemas must both map, and
paths that leave the approved workspace must be refused.
"""
import json
import tempfile
import unittest
from pathlib import Path

from runtime import observation_source as src

TARGET = {"pid": 4242, "create_time": 1789000000.5}


class DeclarationMappingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "ws"
        (self.workspace / "logs").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_saved_declaration_does_not_restore_undeclared_fields(self):
        declaration = {"log_path": "logs/events.jsonl", "fields": {"event": "kind", "pid": "process", "timestamp": "at"}}
        config = src.candidate_to_config(declaration, self.workspace, TARGET)
        path = self.workspace / "binding.json"
        path.write_text(json.dumps(config))
        loaded = src.load_config(path)
        self.assertEqual(loaded["fields"], declaration["fields"])
        self.assertNotIn("call_id", loaded["fields"])

    def test_schema_a_arbitrary_field_names_map(self):
        declaration = {
            "log_path": "logs/probe.jsonl",
            "fields": {"event": "kind", "pid": "process", "timestamp": "at",
                       "tool": "toolName", "call_id": "invocation"},
            "event_names": {"hook.loaded": "probe.ready",
                            "tool.execute.before": "probe.tool.before",
                            "tool.execute.after": "probe.tool.after"},
        }
        config = src.candidate_to_config(declaration, self.workspace, TARGET)

        import os
        self.assertEqual(config["log_path"], os.path.abspath(self.workspace / "logs" / "probe.jsonl"))
        self.assertEqual(config["fields"]["event"], "kind")
        self.assertEqual(config["fields"]["call_id"], "invocation")
        self.assertEqual(config["event_names"]["hook.loaded"], "probe.ready")
        self.assertEqual(config["target"], TARGET)

        # The produced config must actually drive the real reader for that schema.
        log = self.workspace / "logs" / "probe.jsonl"
        lines = [
            json.dumps({"kind": "probe.ready", "process": 4242, "at": "2026-09-11T04:10:00Z"}),
            json.dumps({"kind": "probe.tool.before", "process": 4242, "at": "2026-09-11T04:10:01Z",
                        "toolName": "read", "invocation": "c-1"}),
            json.dumps({"kind": "probe.tool.after", "process": 4242, "at": "2026-09-11T04:10:02Z",
                        "toolName": "read", "invocation": "c-1"}),
        ]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        src.reset_state()
        snap = src.snapshot(config)
        self.assertEqual(snap["events"]["valid"], 3)
        self.assertEqual(snap["events"]["invalid"], 0)
        self.assertEqual(snap["paired_calls"], [{"call_id": "c-1", "tool_name": "read"}])

    def test_schema_b_flat_names_also_map(self):
        declaration = {
            "log_path": "hook.ndjson",
            "fields": {"event": "type", "pid": "pid", "timestamp": "time"},
        }
        config = src.candidate_to_config(declaration, self.workspace, TARGET)
        self.assertEqual(config["fields"]["event"], "type")
        self.assertEqual(config["event_names"]["tool.execute.before"], "tool.execute.before")
        # Optional roles must not be invented for the candidate.
        self.assertNotIn("call_id", config["fields"])
        self.assertNotIn("tool", config["fields"])

        # A tool event with no declared correlation role is invalid, and the
        # reader must not raise on the missing role.
        log = self.workspace / "hook.ndjson"
        log.write_text("\n".join([
            json.dumps({"type": "hook.loaded", "pid": 4242, "time": "2026-09-11T04:20:00Z"}),
            json.dumps({"type": "tool.execute.before", "pid": 4242, "time": "2026-09-11T04:20:01Z"}),
        ]) + "\n", encoding="utf-8")
        src.reset_state()
        snap = src.snapshot(config)
        self.assertEqual(snap["events"]["valid"], 1)
        self.assertEqual(snap["events"]["invalid"], 1)

    def test_two_declarations_in_one_workspace_stay_independent(self):
        one = src.candidate_to_config(
            {"log_path": "a.jsonl", "fields": {"event": "e1", "pid": "p", "timestamp": "t"}},
            self.workspace, TARGET)
        two = src.candidate_to_config(
            {"log_path": "b.jsonl", "fields": {"event": "e2", "pid": "p", "timestamp": "t"}},
            self.workspace, TARGET)
        self.assertNotEqual(one["log_path"], two["log_path"])
        self.assertEqual(one["fields"]["event"], "e1")
        self.assertEqual(two["fields"]["event"], "e2")
class DeclarationRejectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "ws"
        self.workspace.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def base(self, **overrides) -> dict:
        value = {"log_path": "probe.jsonl",
                 "fields": {"event": "event", "pid": "pid", "timestamp": "ts"}}
        value.update(overrides)
        return value

    def test_parent_traversal_is_rejected(self):
        for path in ("../outside.jsonl", "logs/../../outside.jsonl"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as ctx:
                    src.candidate_to_config(self.base(log_path=path), self.workspace, TARGET)
                self.assertIn("inside the workspace", str(ctx.exception))

    def test_absolute_path_is_rejected(self):
        with self.assertRaises(ValueError):
            src.candidate_to_config(self.base(log_path="/tmp/escape.jsonl"), self.workspace, TARGET)

    def test_backslash_and_empty_segments_are_rejected(self):
        for path in ("logs\\probe.jsonl", "logs//probe.jsonl", "./probe.jsonl", ""):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    src.candidate_to_config(self.base(log_path=path), self.workspace, TARGET)

    def test_declaring_a_target_is_refused(self):
        for key, value in (("target", TARGET), ("pid", 4242), ("create_time", 1.0)):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    src.candidate_to_config(self.base(**{key: value}), self.workspace, TARGET)
                self.assertIn("executor binds", str(ctx.exception))

    def test_unknown_field_role_is_rejected(self):
        bad = {"event": "e", "pid": "p", "timestamp": "t", "session": "s"}
        with self.assertRaises(ValueError) as ctx:
            src.candidate_to_config(self.base(fields=bad), self.workspace, TARGET)
        self.assertIn("unknown observation_source.fields key", str(ctx.exception))

    def test_missing_required_field_role_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            src.candidate_to_config(self.base(fields={"event": "e", "pid": "p"}),
                                    self.workspace, TARGET)
        self.assertIn("must declare: timestamp", str(ctx.exception))

    def test_unknown_event_name_key_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            src.candidate_to_config(self.base(event_names={"agent.started": "x"}),
                                    self.workspace, TARGET)
        self.assertIn("unknown observation_source.event_names key", str(ctx.exception))

    def test_two_roles_sharing_one_field_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            src.candidate_to_config(self.base(fields={"event": "x", "pid": "x", "timestamp": "t"}),
                                    self.workspace, TARGET)
        self.assertIn("same field", str(ctx.exception))

    def test_target_binding_is_validated_by_the_caller(self):
        for bad in ({}, {"pid": 1}, {"pid": 0, "create_time": 1.0}, {"pid": "x", "create_time": 1.0}):
            with self.subTest(target=bad):
                with self.assertRaises(ValueError):
                    src.candidate_to_config(self.base(), self.workspace, bad)

    def test_workspace_must_be_absolute_and_present(self):
        with self.assertRaises(ValueError):
            src.candidate_to_config(self.base(), "relative/ws", TARGET)
        with self.assertRaises(ValueError):
            src.candidate_to_config(self.base(), self.workspace / "missing", TARGET)
class RecipeGateTests(unittest.TestCase):
    """The recipe validator must reject a bad declaration before evidence is read."""

    def test_valid_declaration_passes_and_bad_path_is_rejected_by_the_gate(self):
        from runtime.recipe_validation import validate

        def recipe(declaration):
            return {"agent_identity_name": "fixture", "match_features": {"runtime": "fixture"},
                    "observation": "fixture", "fallback": "fixture",
                    "hook": {"method": "unsupported", "restart_required": "unknown",
                             "capabilities": [], "verification": "v", "rollback": "r", "limitations": []},
                    "evidence_refs": ["ev-1-0123456789"], "observation_source": declaration}

        good = {"log_path": "logs/probe.jsonl",
                "fields": {"event": "kind", "pid": "process", "timestamp": "at"}}
        # A valid declaration is not what fails: the later evidence read does.
        # The point is that the declaration gate itself raised nothing.
        with self.assertRaises(Exception) as ctx:
            validate(recipe(good), Path("."))
        self.assertNotIn("observation_source", str(ctx.exception))

        for bad in ({"log_path": "../escape.jsonl",
                     "fields": {"event": "e", "pid": "p", "timestamp": "t"}},
                    {"log_path": "probe.jsonl", "fields": {"event": "e", "pid": "p", "timestamp": "t"},
                     "target": {"pid": 1, "create_time": 1.0}}):
            with self.subTest(declaration=bad):
                with self.assertRaises(ValueError) as ctx:
                    validate(recipe(bad), Path("."))
                self.assertIn("Invalid observation_source", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
