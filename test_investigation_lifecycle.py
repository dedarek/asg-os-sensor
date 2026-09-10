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


if __name__ == "__main__":
    unittest.main()
