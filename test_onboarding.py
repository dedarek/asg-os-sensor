"""受控自动接入纵向切片测试。

其中 Node 插件测试执行仓库内真实 asg-observe.js 和真实事件校验器，但 Goose 配方
来源明确标为 simulated；它不冒充真实 Goose 或真实 OpenCode 产品验收。
"""
import json
import os
import shutil
import subprocess
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
        done = self.root / "done"
        runner = r"""
const fs = require('node:fs');
(async () => {
  while (!fs.existsSync(process.env.READY)) await new Promise(r => setTimeout(r, 20));
  const plugin = require(process.env.PLUGIN_PATH);
  const hooks = await plugin({ directory: process.cwd() });
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
