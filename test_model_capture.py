"""Focused tests for the explicitly enabled model transport recorder."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import psutil

from runtime import model_capture


class ModelCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pid = os.getpid()
        self.create_time = psutil.Process(self.pid).create_time()
        self.target_file = self.root / "target.json"
        self.log_file = self.root / "model-capture.jsonl"
        self.target_file.write_text(json.dumps({
            "pid": self.pid,
            "create_time": self.create_time,
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def configured(self):
        return patch.dict(os.environ, {
            model_capture.TARGET_FILE_ENV: str(self.target_file),
            model_capture.LOG_ENV: str(self.log_file),
        })

    def connection(self, local_port=51001, server_port=43111):
        return SimpleNamespace(
            laddr=SimpleNamespace(ip="127.0.0.1", port=local_port),
            raddr=SimpleNamespace(ip="127.0.0.1", port=server_port),
        )

    def begin(self, *, local_port=51001, server_port=43111, connections=None):
        peer = ("127.0.0.1", local_port)
        connections = [self.connection(local_port, server_port)] if connections is None else connections
        with patch.object(psutil.Process, "net_connections", return_value=connections):
            return model_capture.begin_capture(peer=peer, server_port=server_port)

    def records(self):
        return [json.loads(line) for line in self.log_file.read_text(encoding="utf-8").splitlines()]

    def test_disabled_without_both_explicit_paths_does_not_record(self):
        with patch.dict(os.environ, {
            model_capture.TARGET_FILE_ENV: "",
            model_capture.LOG_ENV: "",
        }):
            with patch.object(psutil, "Process") as process:
                self.assertIsNone(model_capture.begin_capture(peer=("127.0.0.1", 51001), server_port=43111))
                process.assert_not_called()
        self.assertFalse(self.log_file.exists())

    def test_pid_reuse_is_rejected_before_connection_capture(self):
        self.target_file.write_text(json.dumps({
            "pid": self.pid,
            "create_time": self.create_time + 1.0,
        }), encoding="utf-8")
        with self.configured():
            with patch.object(psutil.Process, "net_connections") as net_connections:
                self.assertIsNone(self.begin())
                net_connections.assert_not_called()
        self.assertFalse(self.log_file.exists())

    def test_other_proxy_client_is_rejected(self):
        wrong_client = self.connection(local_port=52002, server_port=43111)
        with self.configured():
            with patch.object(psutil.Process, "net_connections", return_value=[wrong_client]) as net_connections:
                capture = model_capture.begin_capture(peer=("127.0.0.1", 52001), server_port=43111)
                self.assertIsNone(capture)
                net_connections.assert_called_once_with(kind="tcp")
        self.assertFalse(self.log_file.exists())

    def test_json_request_and_sse_response_are_redacted_and_complete(self):
        with self.configured():
            capture = self.begin()
        self.assertIsNotNone(capture)
        request_body = json.dumps({
            "model": "demo",
            "authorization": "Bearer request-secret",
            "messages": [{"role": "user", "content": "hello"}],
        }).encode("utf-8")
        capture.request(request_body, {
            "method": "POST",
            "url": "https://user:password@example.test/v1/chat?token=secret#fragment",
            "content_type": "application/json",
            "transport_side": "agent-facing",
            "authorization": "Bearer header-secret",
        })
        first = b'data: {"choices":[{"delta":{"content":"Bearer response-secret"}}]}\n\n'
        second = b"data: [DONE]\n\n"
        self.assertIs(capture.feed(first), first)
        self.assertIs(capture.feed(second), second)
        capture.finish(metadata={
            "status": 200,
            "content_type": "text/event-stream",
            "transport_side": "agent-facing",
            "authorization": "Bearer finish-secret",
        })

        rows = self.records()
        self.assertEqual([row["event"] for row in rows], ["model.request", "model.response"])
        self.assertEqual(rows[0]["request_id"], rows[1]["request_id"])
        for row in rows:
            self.assertEqual(row["pid"], self.pid)
            self.assertEqual(row["create_time"], self.create_time)
            self.assertEqual(row["capture_layer"], "transport")
            self.assertEqual(row["source"], "explicit_model_proxy")
            self.assertFalse(row["truncated"])
            self.assertTrue(row["body_complete"])
            self.assertNotIn("authorization", row)
        self.assertEqual(rows[0]["body"]["authorization"], "[REDACTED]")
        self.assertEqual(rows[0]["url"], "https://example.test/v1/chat")
        self.assertEqual(rows[1]["status"], 200)
        self.assertNotIn("response-secret", json.dumps(rows[1]["body"]))
        self.assertIn("[REDACTED]", rows[1]["body"])
        self.assertEqual(stat.S_IMODE(self.log_file.stat().st_mode), 0o600)

    def test_request_and_response_are_bounded_and_marked_truncated(self):
        with self.configured():
            capture = self.begin()
        with patch.object(model_capture, "MAX_BODY_BYTES", 8):
            capture.request(b"0123456789", {"method": "POST"})
            chunk = b"abcdefghijk"
            self.assertIs(capture.feed(chunk), chunk)
            capture.finish()
        rows = self.records()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["truncated"])
        self.assertFalse(rows[0]["body_complete"])
        self.assertTrue(rows[1]["truncated"])
        self.assertFalse(rows[1]["body_complete"])

    def test_write_failure_does_not_escape_capture_methods(self):
        with self.configured():
            capture = self.begin()
        with patch.object(model_capture, "_append_record", side_effect=OSError("read only")):
            capture.request(b"{}")
            self.assertEqual(capture.feed(b"data: {}\n\n"), b"data: {}\n\n")
            capture.finish()
        self.assertFalse(self.log_file.exists())

    def test_finish_keeps_frozen_identity_after_target_liveness_changes(self):
        with self.configured():
            capture = self.begin()
        capture.request(b"{}")
        with patch.object(psutil.Process, "create_time", side_effect=psutil.NoSuchProcess(self.pid)):
            capture.finish()
        rows = self.records()
        self.assertEqual([row["pid"] for row in rows], [self.pid, self.pid])
        self.assertEqual([row["create_time"] for row in rows], [self.create_time, self.create_time])


if __name__ == "__main__":
    unittest.main()
