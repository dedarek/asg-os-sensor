"""Focused regressions for the bounded generic raw Hook data surface."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

import psutil

from runtime import hook_data
from runtime.observation_registry import Registry


class HookDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pid = os.getpid()
        self.create_time = psutil.Process(self.pid).create_time()
        self.log = self.root / "hook-events.jsonl"
        config = self.root / "source.json"
        config.write_text(json.dumps({
            "target": {"pid": self.pid, "create_time": self.create_time},
            "log_path": str(self.log),
        }), encoding="utf-8")
        Registry(self.root).register(config, {"pid": self.pid, "create_time": self.create_time})

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, *events):
        with self.log.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def event(self, event_type, *, pid=None, create_time=None, ts=None, **extra):
        value = {
            "ts": self.create_time + 1 if ts is None else ts,
            "pid": self.pid if pid is None else pid,
            "event": event_type,
        }
        if create_time is not None:
            value["create_time"] = create_time
        value.update(extra)
        return value

    def test_returns_raw_payload_after_identity_filter_and_credential_redaction(self):
        self.append(
            self.event("hook.loaded", input={"prompt": "keep this"},
                       authorization="Bearer live-secret", nested={"api_key": "key-secret"}),
            self.event("tool.execute.before", pid=self.pid + 1, token="wrong-pid"),
            self.event("tool.execute.after", create_time=self.create_time + 5,
                       output={"text": "wrong-create-time"}),
            self.event("user.input", output={"text": "actual payload"}),
        )
        result = hook_data.snapshot(self.root, pid=self.pid, create_time=self.create_time)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["records"][0]["payload"]["input"], {"prompt": "keep this"})
        self.assertEqual(result["records"][0]["payload"]["authorization"], "[REDACTED]")
        self.assertEqual(result["records"][0]["payload"]["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(result["records"][1]["payload"]["output"], {"text": "actual payload"})
        self.assertEqual(result["coverage"]["filtered_records"], 2)
        self.assertIn("pid_mismatch", result["bindings"][0]["coverage"]["filtered_reasons"])
        self.assertIn("create_time_mismatch", result["bindings"][0]["coverage"]["filtered_reasons"])

    def test_refresh_reads_appended_records_and_missing_types_are_explicit(self):
        self.append(self.event("hook.loaded"))
        first = hook_data.snapshot(self.root, pid=self.pid)
        self.assertEqual(len(first["records"]), 1)
        self.append(self.event("model.request", ts=self.create_time + 2,
                               input={"messages": [{"role": "user", "content": "hello"}]}))
        second = hook_data.snapshot(self.root, pid=self.pid)
        self.assertEqual(len(second["records"]), 2)
        self.assertIn("model.request", second["coverage"]["observed_event_types"])
        self.assertNotIn("tool.execute.after", second["coverage"]["missing_event_types"])
        self.assertIn("tool.execute.after", second["coverage"]["supported_not_declared_event_types"])
        self.assertFalse(second["coverage"]["complete"])

    def test_native_alias_counts_toward_canonical_coverage(self):
        self.append(self.event("user.prompt.submitted", prompt="hello"))
        result = hook_data.snapshot(self.root, pid=self.pid)
        self.assertEqual(result["coverage"]["counts"]["user.input"], 1)
        self.assertIn("user.input", result["coverage"]["observed_event_types"])
        self.assertNotIn("user.input", result["coverage"]["missing_event_types"])

    def test_limit_and_no_binding_report_bounds_without_fabricating_events(self):
        self.append(self.event("hook.loaded"), self.event("assistant.output", ts=self.create_time + 2))
        result = hook_data.snapshot(self.root, pid=self.pid, limit=1)
        self.assertEqual(len(result["records"]), 1)
        self.assertTrue(result["bindings"][0]["coverage"]["truncated_records"])
        self.assertFalse(result["coverage"]["complete"])

        empty = hook_data.snapshot(self.root.parent / "missing-run-dir", pid=self.pid)
        self.assertEqual(empty["status"], "not_configured")
        self.assertEqual(empty["records"], [])
        self.assertEqual(empty["coverage"]["missing_inputs"], ["observation_binding"])

    def test_model_summaries_do_not_claim_network_capture(self):
        self.append(
            self.event("model.request", input={"messages": [{"role": "user", "content": "hello"}]}),
            self.event("assistant.output", output={"text": "world"}, ts=self.create_time + 2),
        )
        result = hook_data.snapshot(self.root, pid=self.pid)
        network = result["coverage"]["network_capture"]
        self.assertEqual(network["status"], "partial")
        self.assertFalse(network["complete"])
        self.assertFalse(network["captured"])
        self.assertEqual(network["qualified_events"], 0)
        self.assertEqual(network["complete_pairs"], 0)
        self.assertIn("transport_layer", network["missing_requirements"])
        self.assertIn("correlation_id", network["missing_requirements"])
        self.assertIn("body", network["missing_requirements"])
        self.assertFalse(result["coverage"]["network_capture_complete"])

    def test_explicit_transport_request_response_pair_is_complete(self):
        self.append(
            self.event(
                "model.request",
                capture_layer="transport",
                request_id="req-1",
                body={"messages": [{"role": "user", "content": "hello"}]},
                body_complete=True,
                truncated=False,
            ),
            self.event(
                "model.response",
                capture_layer="transport",
                request_id="req-1",
                body={"choices": [{"message": {"content": "world"}}]},
                body_complete=True,
                truncated=False,
                ts=self.create_time + 2,
            ),
        )
        result = hook_data.snapshot(self.root, pid=self.pid)
        network = result["coverage"]["network_capture"]
        self.assertEqual(network["status"], "complete")
        self.assertTrue(network["complete"])
        self.assertTrue(network["captured"])
        self.assertEqual(network["candidate_events"], 2)
        self.assertEqual(network["qualified_events"], 2)
        self.assertEqual(network["request_events"], 1)
        self.assertEqual(network["response_events"], 1)
        self.assertEqual(network["complete_pairs"], 1)
        self.assertTrue(network["requirements"]["transport_layer"])
        self.assertTrue(network["requirements"]["correlation_id"])
        self.assertTrue(network["requirements"]["complete_body"])
        self.assertTrue(network["requirements"]["not_truncated"])
        self.assertTrue(result["bindings"][0]["coverage"]["network_capture_complete"])

    def test_transport_pair_is_not_downgraded_by_model_summary_binding(self):
        second_log = self.root / "transport-hook-events.jsonl"
        second_config = self.root / "transport-source.json"
        second_config.write_text(json.dumps({
            "target": {"pid": self.pid, "create_time": self.create_time + 100},
            "log_path": str(second_log),
        }), encoding="utf-8")
        Registry(self.root).register(second_config, {
            "pid": self.pid, "create_time": self.create_time + 100
        })
        self.append(self.event(
            "model.request", input={"messages": [{"role": "user", "content": "summary"}]}
        ))
        second_log.write_text("\n".join(json.dumps(item) for item in (
            {"ts": self.create_time + 101, "pid": self.pid, "event": "model.request",
             "capture_layer": "transport", "request_id": "pair-1", "body": {"messages": []},
             "body_complete": True, "truncated": False},
            {"ts": self.create_time + 102, "pid": self.pid, "event": "model.response",
             "capture_layer": "transport", "request_id": "pair-1", "body": {"choices": []},
             "body_complete": True, "truncated": False},
        )) + "\n", encoding="utf-8")
        result = hook_data.snapshot(self.root, pid=self.pid)
        self.assertEqual(result["coverage"]["network_capture"]["status"], "complete")
        self.assertTrue(result["coverage"]["network_capture_complete"])

    def test_truncated_transport_body_cannot_claim_complete_capture(self):
        self.append(
            self.event(
                "model.request",
                capture_layer="transport",
                request_id="req-2",
                body={"messages": [{"role": "user", "content": "hello"}]},
                body_complete=True,
                truncated=True,
            ),
            self.event(
                "model.response",
                capture_layer="transport",
                request_id="req-2",
                body={"choices": [{"message": {"content": "world"}}]},
                body_complete=True,
                truncated=False,
                ts=self.create_time + 2,
            ),
        )
        result = hook_data.snapshot(self.root, pid=self.pid)
        network = result["coverage"]["network_capture"]
        self.assertEqual(network["status"], "partial")
        self.assertFalse(network["complete"])
        self.assertEqual(network["qualified_events"], 1)
        self.assertEqual(network["complete_pairs"], 0)
        self.assertIn("not_truncated", network["missing_requirements"])

    def test_network_pairs_do_not_cross_bindings(self):
        second_create_time = self.create_time + 100
        second_log = self.root / "second-hook-events.jsonl"
        second_config = self.root / "second-source.json"
        second_config.write_text(json.dumps({
            "target": {"pid": self.pid, "create_time": second_create_time},
            "log_path": str(second_log),
        }), encoding="utf-8")
        Registry(self.root).register(
            second_config, {"pid": self.pid, "create_time": second_create_time}
        )
        self.append(self.event(
            "model.request", capture_layer="transport", request_id="shared",
            body={"messages": []}, body_complete=True, truncated=False,
        ))
        second_log.write_text(json.dumps({
            "ts": second_create_time + 1,
            "pid": self.pid,
            "event": "model.response",
            "capture_layer": "transport",
            "request_id": "shared",
            "body": {"choices": []},
            "body_complete": True,
            "truncated": False,
        }) + "\n", encoding="utf-8")

        result = hook_data.snapshot(self.root, pid=self.pid)
        network = result["coverage"]["network_capture"]
        self.assertEqual(network["candidate_events"], 2)
        self.assertEqual(network["qualified_events"], 2)
        self.assertEqual(network["request_events"], 1)
        self.assertEqual(network["response_events"], 1)
        self.assertEqual(network["complete_pairs"], 0)
        self.assertEqual(network["status"], "partial")
        self.assertFalse(network["complete"])
        self.assertTrue(all(
            item["coverage"]["network_capture"]["complete_pairs"] == 0
            for item in result["bindings"]
            if item.get("coverage")
        ))


if __name__ == "__main__":
    unittest.main()
