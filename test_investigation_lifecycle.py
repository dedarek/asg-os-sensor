import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

import monitor_dashboard as dashboard


class InvestigationLifecycleTests(unittest.TestCase):
    def _run_fake_goose(self, mode: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "goose-fixture"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys, time\n"
                "if os.environ.get('FAKE_GOOSE_MODE') == 'timeout': time.sleep(3)\n"
                "sys.exit(7)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            target = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                target_identity = {"pid": target.pid, "create_time": psutil.Process(target.pid).create_time()}
                run_root = root / "runs"
                temp_db = root / "fingerprints.json"
                route = {"route": "fixture", "provider": "openai", "model": "fixture-model",
                         "base_url": "http://127.0.0.1:9/v1", "key_env": "FIXTURE_KEY"}
                with patch.object(dashboard, "AUTONOMOUS_ANALYSIS_ENABLED", True), \
                     patch.object(dashboard, "GOOSE", fake), \
                     patch.object(dashboard, "GOOSE_TIMEOUT_S", 0.1 if mode == "timeout" else 5), \
                     patch.object(dashboard, "GOOSE_MAX_TURNS", 18), \
                     patch.object(dashboard, "GOOSE_MAX_TOOL_REPETITIONS", 4), \
                     patch.object(dashboard, "analyst_route", return_value=route), \
                     patch.object(dashboard, "load_analyst_key", return_value="fixture-key"), \
                     patch.object(dashboard, "build_goose_env", return_value={"FAKE_GOOSE_MODE": mode}), \
                     patch.object(dashboard, "tls_exception_enabled", return_value=False), \
                     patch.object(dashboard, "_record_onboarding_outcome"), \
                     patch.object(dashboard.matcher, "db_path", return_value=temp_db), \
                     patch.dict(os.environ, {"ASG_RUN_DIR": str(run_root)}, clear=False):
                    struct = {"pid": target.pid, "create_time": target_identity["create_time"],
                              "compatibility": {"runtime": "fixture"}}
                    dashboard.run_autonomous_investigation(target.pid, struct, force=True)

                run_dirs = list(run_root.glob(f"pid_{target.pid}_*"))
                self.assertEqual(len(run_dirs), 1)
                lifecycle = json.loads((run_dirs[0] / "investigation_lifecycle.json").read_text())
                self.assertEqual(lifecycle["target"], target_identity)
                self.assertIsInstance(lifecycle["goose_process"]["pid"], int)
                self.assertIsNotNone(lifecycle["goose_process"]["create_time"])
                self.assertIsNotNone(lifecycle["returncode"])
                self.assertEqual(lifecycle["timed_out"], mode == "timeout")
                self.assertEqual(lifecycle["end_reason"], "timeout" if mode == "timeout" else "nonzero_exit")
                self.assertEqual(lifecycle["resume"]["status"], "available_from_saved_evidence")
                self.assertNotEqual(lifecycle["status"], "unsupported")
            finally:
                target.terminate()
                target.wait(timeout=3)

    def test_nonzero_exit_lifecycle_is_persisted(self):
        self._run_fake_goose("nonzero")

    def test_timeout_lifecycle_keeps_progress_and_reason(self):
        self._run_fake_goose("timeout")

    def test_unset_deadline_does_not_add_wrapper_timeout_or_kill_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "goose-fixture"
            fake.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(7)\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                ct = psutil.Process(target.pid).create_time()
                route = {"route": "fixture", "provider": "openai", "model": "fixture-model",
                         "base_url": "http://127.0.0.1:9/v1", "key_env": "FIXTURE_KEY"}
                with patch.object(dashboard, "AUTONOMOUS_ANALYSIS_ENABLED", True), \
                     patch.object(dashboard, "GOOSE", fake), \
                     patch.object(dashboard, "GOOSE_TIMEOUT_S", None), \
                     patch.object(dashboard, "GOOSE_MAX_TURNS", None), \
                     patch.object(dashboard, "GOOSE_MAX_TOOL_REPETITIONS", None), \
                     patch.object(dashboard, "analyst_route", return_value=route), \
                     patch.object(dashboard, "load_analyst_key", return_value="fixture-key"), \
                     patch.object(dashboard, "build_goose_env", return_value={}), \
                     patch.object(dashboard, "tls_exception_enabled", return_value=False), \
                     patch.object(dashboard, "_record_onboarding_outcome"), \
                     patch.dict(os.environ, {"ASG_RUN_DIR": str(root / "runs")}, clear=False):
                    dashboard.run_autonomous_investigation(
                        target.pid, {"pid": target.pid, "create_time": ct, "compatibility": {}}, force=True)
                run_dir = next((root / "runs").glob(f"pid_{target.pid}_*"))
                lifecycle = json.loads((run_dir / "investigation_lifecycle.json").read_text())
                self.assertIsNone(lifecycle["timeout_seconds"])
                self.assertFalse(lifecycle["timeout_enabled"])
                self.assertFalse(lifecycle["timed_out"])
                self.assertEqual(lifecycle["end_reason"], "nonzero_exit")
                self.assertNotIn("--max-turns", lifecycle["command"])
                self.assertNotIn("--max-tool-repetitions", lifecycle["command"])
            finally:
                target.terminate()
                target.wait(timeout=3)

    def test_stream_journal_is_bounded_and_keeps_only_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analyst_stdout.jsonl"
            journal = dashboard._StreamJournal(path, 64 * 1024)
            raw = '{"type":"message","message":{"id":"same","created":1,"role":"assistant","content":[{"type":"thinking","thinking":"PRIVATE-THOUGHT"},{"type":"text","text":"PRIVATE-TEXT"}]}}\n'
            for _ in range(5000):
                journal.consume(raw)
            stats = journal.close()
            self.assertGreater(stats["input_bytes"], stats["journal_bytes"])
            self.assertEqual(stats["unique_message_ids"], 1)
            self.assertGreater(stats["rotations"], 0)
            self.assertNotIn("PRIVATE-THOUGHT", path.read_text(encoding="utf-8"))
            self.assertNotIn("PRIVATE-TEXT", path.read_text(encoding="utf-8"))

    def test_continuation_passes_prior_bounded_summary_to_real_extension_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "goose-fixture"
            marker = root / "extension-result.json"
            tools_path = Path(__file__).resolve().parent / "runtime" / "analyst_tools.py"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, subprocess, sys\n"
                "request = json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_saved_investigation','arguments':{}}}) + '\\n'\n"
                "out = subprocess.run([sys.executable, '-B', os.environ['FAKE_TOOLS']], input=request, text=True, capture_output=True, env=os.environ).stdout\n"
                "open(os.environ['FAKE_MARKER'], 'w').write(out)\n"
                "sys.exit(7)\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                ct = psutil.Process(target.pid).create_time()
                run_root = root / "runs"
                previous = run_root / f"pid_{target.pid}_previous"
                (previous / "evidence").mkdir(parents=True)
                (previous / "investigation_lifecycle.json").write_text(json.dumps({
                    "status": "timeout", "end_reason": "timeout",
                    "target": {"pid": target.pid, "create_time": ct},
                    "tool_call_count": 1,
                }), encoding="utf-8")
                (previous / "analyst_tool_calls.jsonl").write_text(
                    json.dumps({"tool": "inspect_entry_surface", "evidence_id": "ev-1-abcdef1234"}) + "\n",
                    encoding="utf-8")
                (previous / "evidence" / "ev-1-abcdef1234.json").write_text(json.dumps({
                    "tool": "inspect_entry_surface", "target": {"pid": target.pid, "create_time": ct},
                    "result": {"entry_candidates": [{"path": "random-agent"}]},
                }), encoding="utf-8")
                (previous / "investigation_findings.json").write_text(json.dumps({
                    "version": 1, "target": {"pid": target.pid, "create_time": ct},
                    "findings": {"identity": {"kind": "identity", "status": "identified",
                        "value": {"name": "prior-random-agent"}, "evidence_refs": ["ev-1-abcdef1234"]},
                        "assets": {}}, "open_questions": ["hook entry"], "history": [],
                }), encoding="utf-8")
                route = {"route": "fixture", "provider": "openai", "model": "fixture-model",
                         "base_url": "http://127.0.0.1:9/v1", "key_env": "FIXTURE_KEY"}
                with patch.object(dashboard, "AUTONOMOUS_ANALYSIS_ENABLED", True), \
                     patch.object(dashboard, "GOOSE", fake), \
                     patch.object(dashboard, "GOOSE_TIMEOUT_S", 5), \
                     patch.object(dashboard, "GOOSE_MAX_TURNS", None), \
                     patch.object(dashboard, "GOOSE_MAX_TOOL_REPETITIONS", None), \
                     patch.object(dashboard, "analyst_route", return_value=route), \
                     patch.object(dashboard, "load_analyst_key", return_value="fixture-key"), \
                     patch.object(dashboard, "build_goose_env", return_value={"FAKE_TOOLS": str(tools_path), "FAKE_MARKER": str(marker)}), \
                     patch.object(dashboard, "tls_exception_enabled", return_value=False), \
                     patch.object(dashboard, "_record_onboarding_outcome"), \
                     patch.dict(os.environ, {"ASG_RUN_DIR": str(run_root)}, clear=False):
                    dashboard.run_autonomous_investigation(
                        target.pid, {"pid": target.pid, "create_time": ct, "compatibility": {}},
                        force=True, resume_from=previous)
                current = max(run_root.glob(f"pid_{target.pid}_*"), key=lambda path: path.stat().st_mtime)
                lifecycle = json.loads((current / "investigation_lifecycle.json").read_text())
                self.assertEqual(lifecycle["resume"]["source"], str(previous))
                self.assertEqual(lifecycle["resume"]["details"]["evidence_files_copied"], 1)
                self.assertTrue((current / "evidence" / "ev-1-abcdef1234.json").exists())
                self.assertIn("prior-random-agent", marker.read_text(encoding="utf-8"))
                self.assertIn("hook entry", marker.read_text(encoding="utf-8"))
            finally:
                target.terminate()
                target.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
