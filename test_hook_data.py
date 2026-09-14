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


if __name__ == "__main__":
    unittest.main()
