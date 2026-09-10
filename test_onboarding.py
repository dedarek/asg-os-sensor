"""受控自动接入纵向切片测试。

其中 Node 插件测试执行仓库内真实 asg-observe.js 和真实事件校验器，但 Goose 配方
来源明确标为 simulated；它不冒充真实 Goose 或真实 OpenCode 产品验收。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from runtime import matcher, onboarding
from runtime.compatibility import observe


ROOT = Path(__file__).resolve().parent
NODE = os.environ.get("ASG_TEST_NODE") or shutil.which("node") or ""


class OnboardingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "ASG_FINGERPRINT_DB": str(self.root / "fingerprints.json"),
            "ASG_EXPERIENCE_DB": str(self.root / "experience.json"),
            "ASG_TEST_SIMULATED": "1",
        }, clear=False)
        self.env.start()
        self.exe = self.root / "runtime-agent"
        self.exe.write_bytes(b"runtime-agent-build-v1")
        self.target = {"pid": os.getpid(), "create_time": psutil.Process().create_time()}
        self.struct = {
            "pid": self.target["pid"], "create_time": self.target["create_time"],
            "exe": "runtime-agent", "runtime": "native", "argv_shape": [],
            "config_dirs": [], "cwd": str(self.root),
            "compatibility": observe(str(self.exe), [str(self.exe)], str(self.root)),
        }
        self.recipe = {
            "agent_identity_name": "runtime-agent",
            "evidence_refs": ["ev-1-0123456789"],
            "match_features": {"runtime": "native"},
            "observation": "bound process evidence",
            "hook": {
                "adapter": "opencode-workspace-plugin",
                "method": "workspace-plugin",
                "scope": "project",
                "installer": "runtime.opencode.ghost_install",
                "plugin_name": "asg-observe.js",
                "activation_event": "hook.loaded",
                "restart_required": True,
                "capabilities": ["observe tool lifecycle"],
                "verification": "check bound hook.loaded event",
                "rollback": "run the verified installer uninstall operation",
                "limitations": ["startup or reload required"],
            },
            "fallback": "unsupported",
            "confidence": 0.8,
        }

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_exact_plan_reuses_recipe_without_goose(self):
        entry = matcher.remember_verified(
            self.struct, self.recipe,
            [{"evidence_id": "ev-1-0123456789", "tool": "get_target_context"}],
        )
        matched = matcher.classify(self.struct)
        self.assertEqual(matched["status"], "exact")
        plan = onboarding.plan_from_match(self.struct, matched)
        self.assertEqual(plan["status"], "plan_pending_authorization")
        self.assertEqual(plan["action"], "install_reused_recipe")
        self.assertEqual(plan["recipe_source"], "fingerprint_reuse")
        self.assertEqual(plan["fingerprint_id"], entry["id"])
        self.assertEqual(entry["recipe_source"], "goose")
        self.assertFalse((self.root / "experience.json").exists(), "pure plan must not persist")

    def test_legacy_recipe_without_provenance_cannot_be_reused(self):
        legacy = dict(self.recipe)
        legacy.pop("provenance", None)
        matched = onboarding.plan_from_match(
            self.struct, {"status": "exact", "entry": {
                "id": "legacy-1", "revision": 1, "hook_recipe": legacy,
            }},
        )
        self.assertEqual(matched["status"], "unsupported")
        self.assertIn("manual/legacy", matched["reason"])

    def test_similar_and_unsupported_are_not_install_plans(self):
        entry = matcher.remember_verified(
            self.struct, self.recipe,
            [{"evidence_id": "ev-1-0123456789", "tool": "get_target_context"}],
        )
        changed = dict(self.struct, compatibility=dict(self.struct["compatibility"], launch="changed"))
        similar = onboarding.plan_from_match(changed, matcher.classify(changed))
        self.assertEqual(similar["status"], "investigation_required")
        self.assertEqual(similar["action"], "goose_investigate")

        unsupported_recipe = dict(self.recipe, hook=dict(self.recipe["hook"], scope="host"))
        unsupported = onboarding.plan_from_recipe(
            self.struct, unsupported_recipe, "miss", entry=entry, recipe_source="goose",
        )
        self.assertEqual(unsupported["status"], "unsupported")
        result = onboarding.execute_install(unsupported, self.target, {
            "approved": True, "scope": "host", "workspace": str(self.root),
        })
        self.assertEqual(result["status"], "unsupported")
        self.assertFalse((self.root / ".opencode" / "plugins" / "asg-observe.js").exists())

    @unittest.skipUnless(NODE, "node unavailable; set ASG_TEST_NODE")
    def test_install_and_real_plugin_events_then_persist_experience(self):
        ready = self.root / "ready"
        loaded = self.root / "loaded"
        tools_ready = self.root / "tools-ready"
        done = self.root / "done"
        runner = r"""
const fs = require('node:fs');
(async () => {
  while (!fs.existsSync(process.env.READY)) await new Promise(r => setTimeout(r, 20));
  const plugin = require(process.env.PLUGIN_PATH);
  const hooks = await plugin({ directory: process.cwd() });
  fs.writeFileSync(process.env.LOADED, 'loaded');
  while (!fs.existsSync(process.env.TOOLS_READY)) await new Promise(r => setTimeout(r, 20));
  const input = { tool: 'read', callID: 'onboarding-call' };
  await hooks['tool.execute.before'](input, {});
  await hooks['tool.execute.after'](input);
  fs.writeFileSync(process.env.DONE, 'done');
  setTimeout(() => process.exit(0), 40);
})().catch(err => { console.error(err); process.exit(1); });
"""
        proc = subprocess.Popen(
            [NODE, "-e", runner], cwd=str(self.root),
            env=dict(os.environ, READY=str(ready), DONE=str(done),
                     LOADED=str(loaded), TOOLS_READY=str(tools_ready),
                     PLUGIN_PATH=str(self.root / ".opencode" / "plugins" / "asg-observe.js")),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        try:
            target = {"pid": proc.pid, "create_time": psutil.Process(proc.pid).create_time()}
            struct = dict(self.struct, pid=proc.pid, create_time=target["create_time"])
            plan = onboarding.plan_from_recipe(struct, self.recipe, "miss", recipe_source="goose-simulated")
            install = onboarding.execute_install(plan, target, {
                "approved": True, "scope": "project", "workspace": str(self.root),
            })
            self.assertEqual(install["status"], "installed_pending_activation")
            ready.write_text("go")
            deadline = time.time() + 10
            while time.time() < deadline and not loaded.exists():
                time.sleep(0.05)
            self.assertTrue(loaded.exists(), "plugin did not load")
            loaded_only = onboarding.verify_activation(install, target)
            self.assertEqual(loaded_only["status"], "loaded_verified")
            self.assertTrue(loaded_only["hook_loaded"])
            self.assertFalse(loaded_only["observing"])
            self.assertFalse(loaded_only["hook_verified"])
            tools_ready.write_text("go")
            while time.time() < deadline and not done.exists():
                time.sleep(0.05)
            self.assertTrue(done.exists(), proc.stderr.read() if proc.stderr else "plugin runner did not finish")
            verification = onboarding.verify_activation(install, target)
            self.assertEqual(verification["status"], "events_verified")
            self.assertTrue(verification["hook_loaded"])
            self.assertGreaterEqual(verification["valid_events"], 3)

            data = onboarding.load_experience()
            event_types = [event["event_type"] for event in data["events"]]
            self.assertIn("install_result", event_types)
            self.assertIn("activation_verification", event_types)
            summary = data["instances"][onboarding.make_instance_id(proc.pid, target["create_time"])]
            self.assertEqual(summary["verification"]["status"], "events_verified")
            self.assertNotIn("nonce", json.dumps(data))

            inactive_manifest = {
                "manifest_version": 1, "name": "asg-observe.js", "runid": install["runid"],
                "active": False, "nonce": "not-exposed", "workspace": str(self.root),
            }
            with patch("runtime.onboarding.load_manifest", return_value=(inactive_manifest, None)):
                revoked = onboarding.verify_activation(install, target)
            self.assertEqual(revoked["status"], "revoked")
            self.assertFalse(revoked["hook_verified"])
            self.assertFalse(revoked["observing_verified"])

            second = onboarding.execute_install(plan, target, {
                "approved": True, "scope": "project", "workspace": str(self.root),
            })
            self.assertEqual(second["status"], "already_installed")
            self.assertEqual(second["runid"], install["runid"])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            if proc.stderr:
                proc.stderr.close()

    def test_experience_mcp_subprocess_reads_isolated_history(self):
        from runtime import analyzer as runtime_analyzer
        target_proc = subprocess.Popen([shutil.which("sleep") or "/bin/sleep", "20"],
                                       cwd=str(self.root), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
        try:
            target = {"pid": target_proc.pid, "create_time": psutil.Process(target_proc.pid).create_time()}
            compatibility = runtime_analyzer.analyze(target["pid"])["compatibility"]
            self.assertIsInstance(compatibility, dict)
            onboarding.record_transition(target, "investigation_failed", {
                "match_status": "similar",
                "reason": "isolated-experience-marker",
                "compatibility": compatibility,
            })
            onboarding.record_transition(target, "activation_verification", {
                "compatibility": compatibility,
                "verification": {
                    "status": "loaded_verified",
                    "hook_loaded": True,
                    "observing": False,
                    "hook_verified": False,
                },
            })
            audit = self.root / "mcp-audit"
            env = dict(os.environ,
                       ASG_EXPERIENCE_DB=str(self.root / "experience.json"),
                       ASG_FINGERPRINT_DB=str(self.root / "different-fingerprint.json"),
                       ASG_AUDIT_DIR=str(audit),
                       ASG_RECIPE_DIR=str(self.root / "mcp-recipes"),
                       ASG_TARGET_PID=str(target["pid"]),
                       ASG_TARGET_CREATE_TIME=str(target["create_time"]))
            env.pop("PYTHONPATH", None)
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "get_prior_experience", "arguments": {}}},
            ]
            proc = subprocess.Popen(
                [sys.executable, "-B", str(ROOT / "runtime" / "analyst_tools.py")],
                cwd=str(self.root), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                stdout, stderr = proc.communicate(
                    "\n".join(json.dumps(message) for message in messages) + "\n", timeout=5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)
            self.assertEqual(proc.returncode, 0, stderr)
            replies = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            reply = next(item for item in replies if item.get("id") == 2)
            text = reply["result"]["content"][0]["text"]
            self.assertIn("isolated-experience-marker", text)
            self.assertIn("loaded_verified", text)
            self.assertIn('"matched_by": "compatibility"', text)
            self.assertIn(str(target["pid"]), text)
            self.assertNotIn(str(self.root), text, "prior projection must not expose artifact paths")
            self.assertTrue((audit / "analyst_tool_calls.jsonl").exists())
        finally:
            if target_proc.poll() is None:
                target_proc.terminate()
                target_proc.wait(timeout=3)

    def test_prior_experience_matches_compatible_new_instance_only(self):
        """兼容运行时可读家族历史；入口/构建变化不得借同名或同 PID 复用。"""
        first = {"pid": 4101, "create_time": 100.0}
        second = {"pid": 4102, "create_time": 200.0}
        compatibility = {
            "executable": "digest-a", "entry": "native", "platform": "Darwin",
            "architecture": "arm64", "runtime": "native", "launch": "launch-a",
            "entry_path": "/private/isolated/runtime-a",
        }
        onboarding.record_transition(first, "investigation_recipe_saved", {
            "match_status": "miss", "fingerprint_id": "family-1", "fingerprint_revision": 3,
            "recipe_source": "goose", "compatibility": compatibility,
            "plan": {"status": "investigation_required", "action": "goose_investigate"},
        })
        prior = onboarding.load_prior_experience(
            instance_id=onboarding.make_instance_id(second["pid"], second["create_time"]),
            compatibility=compatibility,
        )
        self.assertEqual(prior["matched_by"], "compatibility")
        self.assertEqual(prior["matched_instances"], ["4101:100.0"])
        self.assertEqual(prior["recent"][0]["fingerprint_revision"], 3)
        self.assertEqual(prior["recent"][0]["recipe_source"], "goose")
        self.assertNotIn("entry_path", json.dumps(prior))

        incompatible = dict(compatibility, executable="digest-b")
        rejected = onboarding.load_prior_experience(
            instance_id=onboarding.make_instance_id(second["pid"], second["create_time"]),
            compatibility=incompatible,
        )
        self.assertEqual(rejected["matched_by"], "none")
        self.assertEqual(rejected["matched_instances"], [])
        self.assertEqual(rejected["recent"], [])

    @unittest.skipUnless(NODE, "node unavailable; set ASG_TEST_NODE")
    def test_existing_install_rebinds_to_new_pid_and_create_time(self):
        runner = r"""
const fs = require('node:fs');
(async () => {
  while (!fs.existsSync(process.env.READY)) await new Promise(r => setTimeout(r, 20));
  const plugin = require(process.env.PLUGIN_PATH);
  const hooks = await plugin({ directory: process.cwd() });
  fs.writeFileSync(process.env.LOADED, 'loaded');
  while (!fs.existsSync(process.env.TOOLS_READY)) await new Promise(r => setTimeout(r, 20));
  const input = { tool: 'read', callID: process.env.CALL_ID };
  await hooks['tool.execute.before'](input, {});
  await hooks['tool.execute.after'](input);
  fs.writeFileSync(process.env.DONE, 'done');
  setTimeout(() => process.exit(0), 40);
})().catch(err => { console.error(err); process.exit(1); });
"""

        def start(label):
            ready = self.root / (label + "-ready")
            loaded = self.root / (label + "-loaded")
            tools_ready = self.root / (label + "-tools")
            done = self.root / (label + "-done")
            proc = subprocess.Popen(
                [NODE, "-e", runner], cwd=str(self.root),
                env=dict(os.environ, READY=str(ready), LOADED=str(loaded),
                         TOOLS_READY=str(tools_ready), DONE=str(done),
                         CALL_ID="call-" + label,
                         PLUGIN_PATH=str(self.root / ".opencode" / "plugins" / "asg-observe.js")),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            return proc, ready, loaded, tools_ready, done

        def wait_for(path, proc):
            deadline = time.time() + 8
            while time.time() < deadline and not path.exists():
                time.sleep(0.03)
            self.assertTrue(path.exists(), "runner did not reach " + path.name)
            self.assertIsNone(proc.poll(), "runner exited before " + path.name)

        first, first_ready, first_loaded, first_tools, first_done = start("first")
        try:
            first_target = {"pid": first.pid, "create_time": psutil.Process(first.pid).create_time()}
            first_struct = dict(self.struct, pid=first.pid, create_time=first_target["create_time"])
            plan = onboarding.plan_from_recipe(first_struct, self.recipe, "miss", recipe_source="goose-simulated")
            install = onboarding.execute_install(plan, first_target, {
                "approved": True, "scope": "project", "workspace": str(self.root),
            })
            self.assertEqual(install["status"], "installed_pending_activation")
            first_ready.write_text("go")
            wait_for(first_loaded, first)
            first_tools.write_text("go")
            wait_for(first_done, first)
            self.assertEqual(onboarding.verify_activation(install, first_target)["status"], "events_verified")
            first.wait(timeout=3)
        finally:
            if first.poll() is None:
                first.terminate()
                first.wait(timeout=3)
            if first.stderr:
                first.stderr.close()

        second, second_ready, second_loaded, second_tools, second_done = start("second")
        try:
            second_target = {"pid": second.pid, "create_time": psutil.Process(second.pid).create_time()}
            second_struct = dict(self.struct, pid=second.pid, create_time=second_target["create_time"])
            second_plan = onboarding.plan_from_recipe(second_struct, self.recipe, "exact", recipe_source="goose-simulated")
            reused = onboarding.execute_install(second_plan, second_target, {
                "approved": True, "scope": "project", "workspace": str(self.root),
            })
            self.assertEqual(reused["status"], "already_installed")
            before_load = onboarding.verify_activation(reused, second_target)
            self.assertEqual(before_load["status"], "pending_restart")
            second_ready.write_text("go")
            wait_for(second_loaded, second)
            loaded_only = onboarding.verify_activation(reused, second_target)
            self.assertEqual(loaded_only["status"], "loaded_verified")
            self.assertFalse(loaded_only["observing"])
            second_tools.write_text("go")
            wait_for(second_done, second)
            verified = onboarding.verify_activation(reused, second_target)
            self.assertEqual(verified["status"], "events_verified")
            self.assertTrue(verified["observing_verified"])
        finally:
            if second.poll() is None:
                second.terminate()
                second.wait(timeout=3)
            if second.stderr:
                second.stderr.close()

    def test_corrupt_experience_is_visible_error_not_empty_history(self):
        path = self.root / "experience.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            onboarding.load_experience()
        view = onboarding.view_for_instance(
            onboarding.make_instance_id(self.target["pid"], self.target["create_time"]),
            self.struct, {"status": "miss", "reason": "new"},
        )
        self.assertEqual(view["status"], "experience_unavailable")
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
