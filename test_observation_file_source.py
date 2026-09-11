# -*- coding: utf-8 -*-
"""Offline regressions for the generic file-event observation source.

Everything here uses a temp JSONL file with a synthetic target. No real
process is required: liveness is patched, and the point of these tests is the
binding, field-mapping and truthfulness contract, not a product integration.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import monitor_dashboard as dashboard
from runtime import observation_source as src

TARGET = {"pid": 4242, "create_time": 1789000000.5}


def _ts(offset: float = 0.0) -> str:
    stamp = datetime.now(timezone.utc).timestamp() + offset
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _loaded(pid: int = 4242) -> dict:
    return {"ts": _ts(), "pid": pid, "hook": "probe", "event": "plugin.loaded"}


def _before(call_id: str, tool: str = "read", pid: int = 4242) -> dict:
    return {"ts": _ts(), "pid": pid, "hook": "probe", "event": "tool.execute.before",
            "tool": tool, "callID": call_id}


def _after(call_id: str, tool: str = "read", pid: int = 4242) -> dict:
    return {"ts": _ts(), "pid": pid, "hook": "probe", "event": "tool.execute.after",
            "tool": tool, "callID": call_id}


class FileEventSourceTests(unittest.TestCase):
    def setUp(self):
        src.reset_state()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.log = self.root / "probe.log"
        self.log.write_text("", encoding="utf-8")
        self.config_path = self.root / "observe.json"
        self.config_path.write_text(json.dumps({
            "version": 1, "target": {"pid": TARGET["pid"], "create_time": TARGET["create_time"]},
            "log_path": str(self.log),
            "fields": {"event": "event", "pid": "pid", "timestamp": "ts",
                       "tool": "tool", "call_id": "callID"},
            "event_names": {"hook.loaded": "plugin.loaded"},
        }), encoding="utf-8")
        self.config = src.load_config(self.config_path)

    def tearDown(self):
        src.reset_state()
        self._tmp.cleanup()

    def append(self, *events):
        with self.log.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read(self, alive=True):
        with patch.object(src, "target_liveness", return_value=alive):
            return src.snapshot(self.config)

    def test_appended_event_is_visible_on_next_read(self):
        first = self.read()
        self.assertEqual(first["events"]["valid"], 0)
        self.assertEqual(first["health"]["status"], "awaiting_events")

        self.append(_loaded())
        second = self.read()

        self.assertEqual(second["events"]["valid"], 1)
        self.assertTrue(second["health"]["loaded_observed"])
        self.assertEqual(second["health"]["status"], "loaded")
        self.assertIsNotNone(second["last_event_time"])

    def test_incremental_read_does_not_double_count(self):
        self.append(_loaded())
        self.read()
        again = self.read()
        self.assertEqual(again["events"]["valid"], 1)

    def test_paired_tool_events_mark_observing(self):
        self.append(_loaded(), _before("call-1"), _after("call-1"))
        snap = self.read()

        self.assertEqual(snap["health"]["status"], "observing")
        self.assertTrue(snap["health"]["healthy"])
        self.assertEqual(snap["events"]["invalid"], 0)
        self.assertEqual(snap["paired_calls"], [{"call_id": "call-1", "tool_name": "read"}])

    def test_blocking_is_always_declared_unsupported(self):
        self.append(_loaded(), _before("call-2"), _after("call-2"))
        snap = self.read()
        self.assertEqual(snap["blocking"]["status"], "unsupported")
        self.assertEqual(snap["capabilities"]["blocking"]["status"], "unsupported")

    def test_other_instance_events_are_invalid(self):
        self.append(_loaded(pid=9999))
        snap = self.read()
        self.assertEqual(snap["events"]["valid"], 0)
        self.assertEqual(snap["events"]["invalid"], 1)

    def test_unmapped_same_instance_events_are_ignored_not_counted(self):
        self.append({"ts": _ts(), "pid": 4242, "event": "session.updated", "id": "evt_1"})
        snap = self.read()
        self.assertEqual(snap["events"]["valid"], 0)
        self.assertEqual(snap["events"]["invalid"], 0)
        self.assertEqual(snap["events"]["ignored"], 1)

    def test_broken_line_is_invalid_but_does_not_stop_later_lines(self):
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write("{ not json\n")
        self.append(_loaded())
        snap = self.read()
        self.assertEqual(snap["events"]["invalid"], 1)
        self.assertEqual(snap["events"]["valid"], 1)

    def test_tool_event_before_loaded_is_invalid(self):
        self.append(_before("call-3"), _after("call-3"))
        snap = self.read()
        self.assertEqual(snap["events"]["valid"], 0)
        self.assertGreaterEqual(snap["events"]["invalid"], 2)

    def test_unpaired_after_is_invalid(self):
        self.append(_loaded(), _after("call-orphan"))
        snap = self.read()
        self.assertEqual(snap["paired_calls"], [])
        self.assertEqual(snap["events"]["invalid"], 1)

    def test_partial_trailing_line_is_not_parsed_until_complete(self):
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_loaded())[:-1])  # no newline, truncated JSON
        before = self.read()
        self.assertEqual(before["events"]["valid"], 0)
        self.assertEqual(before["events"]["invalid"], 0)
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write("}\n")
        after = self.read()
        self.assertEqual(after["events"]["valid"], 1)

    def test_truncated_or_rotated_log_restarts_cleanly(self):
        self.append(_loaded(), _before("call-4"), _after("call-4"))
        self.read()
        self.log.write_text(json.dumps(_loaded()) + "\n", encoding="utf-8")
        snap = self.read()
        self.assertEqual(snap["events"]["valid"], 1)
        self.assertEqual(snap["paired_calls"], [])

    def test_missing_log_is_awaiting_not_an_error(self):
        self.log.unlink()
        snap = self.read()
        self.assertEqual(snap["status"], "connected")
        self.assertEqual(snap["health"]["status"], "awaiting_events")
        self.assertEqual(snap["events"]["valid"], 0)

    def test_dead_target_is_not_reported_healthy(self):
        self.append(_loaded(), _before("call-5"), _after("call-5"))
        snap = self.read(alive=False)
        self.assertFalse(snap["health"]["healthy"])
        self.assertFalse(snap["health"]["loaded_observed"])
        self.assertIn("退出", snap["message"])
        # Evidence is still recorded, but never promoted to current health.
        self.assertEqual(snap["events"]["valid"], 3)

    def test_target_alive_flag_is_projected(self):
        snap = self.read(alive=True)
        self.assertTrue(snap["target_alive"])
        self.assertEqual(snap["instance_create_time"], TARGET["create_time"])

    # ---- narrow boundary fixes requested by review ----

    def test_event_time_must_be_finite_and_not_before_create_time(self):
        self.append(_loaded())
        # Timestamps that pre-date the bound instance, or that are not finite
        # numbers, must not be accepted as observed activity.
        self.append({"ts": "2026-01-01T00:00:00.000Z", "pid": 4242, "event": "tool.execute.before",
                     "tool": "read", "callID": "old-1"})
        self.append({"ts": float("nan"), "pid": 4242, "event": "tool.execute.before",
                     "tool": "read", "callID": "nan-1"})
        self.append({"ts": float("inf"), "pid": 4242, "event": "tool.execute.before",
                     "tool": "read", "callID": "inf-1"})

        snap = self.read()

        self.assertEqual(snap["events"]["valid"], 1)  # only the loaded event
        self.assertEqual(snap["events"]["invalid"], 3)
        self.assertEqual(snap["paired_calls"], [])

    def test_event_at_or_after_create_time_is_accepted(self):
        at_bind = datetime.fromtimestamp(TARGET["create_time"], timezone.utc)
        self.append({"ts": at_bind.isoformat().replace("+00:00", "Z"), "pid": 4242,
                     "event": "plugin.loaded"})
        snap = self.read()
        self.assertEqual(snap["events"]["valid"], 1)

    def test_same_path_file_replaced_with_new_inode_resets_read_state(self):
        self.append(_loaded())
        first = self.read()
        self.assertEqual(first["events"]["valid"], 1)

        # Rotate in place: a different file now lives at the same path and is
        # larger than the previous read offset, so a size-only check would miss it.
        replacement = self.root / "probe.log.new"
        replacement.write_text("\n".join(json.dumps(item) for item in
                                          (_loaded(), _before("call-rot"), _after("call-rot"))) + "\n",
                               encoding="utf-8")
        self.assertGreater(replacement.stat().st_size, self.log.stat().st_size)
        import os
        os.replace(replacement, self.log)

        snap = self.read()
        self.assertEqual(snap["events"]["valid"], 3)
        self.assertEqual(snap["events"]["invalid"], 0)
        self.assertEqual(snap["paired_calls"], [{"call_id": "call-rot", "tool_name": "read"}])

    def test_field_mapping_change_rebuilds_state_instead_of_mixing(self):
        self.append(_loaded(), _before("call-map"), _after("call-map"))
        self.assertEqual(self.read()["events"]["valid"], 3)

        # Same file, different mapping: the prior counts must not be reused, and
        # the new mapping must be applied to a fresh read of the same bytes.
        changed = json.loads(self.config_path.read_text(encoding="utf-8"))
        changed["fields"] = dict(changed["fields"], call_id="callId")
        self.config_path.write_text(json.dumps(changed), encoding="utf-8")
        remapped = src.snapshot(src.load_config(self.config_path))

        self.assertEqual(remapped["events"]["valid"], 1)   # only the loaded event matches
        self.assertEqual(remapped["events"]["invalid"], 2)  # both tool events lack "callId"
        self.assertEqual(remapped["paired_calls"], [])


class ConfigContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, value) -> Path:
        path = self.root / "observe.json"
        path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")
        return path

    def base(self) -> dict:
        return {"target": {"pid": 1, "create_time": 1.0}, "log_path": str(self.root / "x.log")}

    def test_default_field_mapping_matches_declared_schema(self):
        config = src.load_config(self.write(self.base()))
        self.assertEqual(config["fields"]["event"], "event")
        self.assertEqual(config["fields"]["call_id"], "callID")

    def test_missing_target_and_log_path_are_rejected(self):
        with self.assertRaises(ValueError):
            src.load_config(self.write({"log_path": str(self.root / "x.log")}))
        with self.assertRaises(ValueError):
            src.load_config(self.write({"target": {"pid": 1, "create_time": 1.0}}))

    def test_relative_log_path_is_rejected(self):
        value = self.base()
        value["log_path"] = "relative/probe.log"
        with self.assertRaises(ValueError):
            src.load_config(self.write(value))

    def test_unknown_field_or_event_key_is_rejected(self):
        value = self.base()
        value["fields"] = {"nonsense": "x"}
        with self.assertRaises(ValueError):
            src.load_config(self.write(value))
        value = self.base()
        value["event_names"] = {"some.other.event": "x"}
        with self.assertRaises(ValueError):
            src.load_config(self.write(value))

    def test_corrupt_config_is_an_explicit_error(self):
        with self.assertRaises(ValueError):
            src.load_config(self.write("{ not json"))
        missing = self.root / "absent.json"
        with self.assertRaises(ValueError):
            src.load_config(missing)


class DashboardProjectionTests(unittest.TestCase):
    """The scan/API path must expose the same snapshot shape as the HTTP source."""

    def snapshot(self, **overrides):
        base = {"status": "connected", "source": "/tmp/observe.json", "instance_pid": 4242,
                "instance_create_time": 1789000000.5, "last_event_time": 1789000001.0,
                "target_alive": True, "blocking": {"status": "unsupported", "label": "未支持"},
                "health": {"status": "observing", "healthy": True, "loaded_observed": True,
                           "reason": "paired"},
                "events": {"valid": 3, "invalid": 0, "ignored": 5},
                "capabilities": {"observation": {"status": "supported"},
                                 "blocking": {"status": "unsupported", "label": "未支持"}}}
        base.update(overrides)
        return base

    def test_bound_live_instance_is_observed_without_blocking_claim(self):
        view = dashboard.observation_for_instance(self.snapshot(), 4242, 1789000000.5)
        self.assertEqual(view["status"], "observed")
        self.assertEqual(view["recent_events"], 3)
        self.assertEqual(view["blocking"]["status"], "unsupported")
        self.assertTrue(view["target_alive"])

    def test_dead_target_keeps_record_but_is_not_healthy(self):
        view = dashboard.observation_for_instance(
            self.snapshot(target_alive=False,
                          health={"status": "observing", "healthy": False, "loaded_observed": False,
                                  "reason": "目标进程已退出"},
                          message="目标进程已退出或被替换"), 4242, 1789000000.5)
        self.assertEqual(view["status"], "target_gone")
        self.assertFalse(view["loaded_observed"])
        self.assertFalse(view["target_alive"])
        self.assertIn("退出", view["label"])

    def test_other_pid_gets_no_evidence(self):
        view = dashboard.observation_for_instance(self.snapshot(), 9999, 1789000000.5)
        self.assertEqual(view["status"], "not_bound")
        self.assertEqual(view["recent_events"], 0)

    def test_create_time_mismatch_gets_no_evidence(self):
        view = dashboard.observation_for_instance(self.snapshot(), 4242, 1789000999.0)
        self.assertEqual(view["status"], "not_bound")
        self.assertIn("create_time", view["reason"])


class MutualExclusionTests(unittest.TestCase):
    def test_both_sources_configured_is_invalid(self):
        with patch.object(dashboard, "OBSERVE_CONFIG", "/tmp/x.json"), \
             patch.object(dashboard, "OBSERVE_URL", "http://127.0.0.1:1"), \
             patch.object(dashboard, "OBSERVE_CONFIG_ERROR", "冲突"):
            result = dashboard.read_observation_snapshot()
        self.assertEqual(result["status"], "invalid")

    def test_file_source_error_is_surfaced_not_swallowed(self):
        with patch.object(dashboard, "OBSERVE_CONFIG", "/nonexistent/observe.json"), \
             patch.object(dashboard, "OBSERVE_CONFIG_ERROR", ""):
            result = dashboard.read_observation_snapshot()
        self.assertEqual(result["status"], "invalid")
        self.assertIn("unreadable", result["message"])


if __name__ == "__main__":
    unittest.main()
