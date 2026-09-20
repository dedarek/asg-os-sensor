"""Managed Codex hook blocks are trusted only via a hash-matching hooks.state entry."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from integrations.soc_inventory import codex_trust

HOOK = "/usr/bin/env python3 sec_hook.py security-hooks-codex"
OTHER = "/bin/sh /Users/test/.codex/asg-observer/run-hook.sh"

LABEL = {"UserPromptSubmit": "user_prompt_submit", "PreToolUse": "pre_tool_use",
         "PostToolUse": "post_tool_use", "Stop": "stop", "SessionEnd": "session_end"}


def current_hash(event, command=HOOK, timeout=None, matcher=None):
    handler = {"type": "command", "command": command}
    if timeout is not None:
        handler["timeout"] = timeout
    return codex_trust.handler_hash(LABEL[event], matcher, handler)


def with_state(path, blocks, trusted):
    text = ""
    for key in trusted:
        event = [e for e, l in LABEL.items() if l == key][0]
        command = next((c for ev, c in blocks if ev == event), HOOK)
        text += '[hooks.state."%s:%s:0:0"]' % (path, key) + chr(10)
        text += "enabled = true" + chr(10)
        text += 'trusted_hash = "%s"' % current_hash(event, command) + chr(10)
    return text


class CodexTrustTests(unittest.TestCase):
    def audit(self, blocks, trusted_events=(), state_writer=None):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "config.toml"
            text = ""
            for event, command in blocks:
                text += "[[hooks.%s]]" % event + chr(10) + "" + chr(10)
                text += "[[hooks.%s.hooks]]" % event + chr(10)
                text += 'type = "command"' + chr(10)
                text += 'command = "%s"' % command + chr(10)
            if state_writer is not None:
                text += state_writer(real, blocks)
            else:
                text += with_state(str(real), blocks, trusted_events)
            real.write_text(text, encoding="utf-8")
            return codex_trust.audit(real)

    def test_untrusted_without_state(self):
        rep = self.audit([("UserPromptSubmit", HOOK), ("PreToolUse", HOOK)])
        self.assertEqual(rep["status"], "awaiting_user_trust")
        self.assertEqual(rep["untrusted"], 2)
        self.assertTrue(rep["action_required"])
        self.assertTrue(rep["hash_verified"])

    def test_trusted_with_matching_hash(self):
        rep = self.audit([("UserPromptSubmit", HOOK), ("PreToolUse", HOOK)],
                         trusted_events=["user_prompt_submit", "pre_tool_use"])
        self.assertEqual(rep["status"], "trusted")
        self.assertFalse(rep["action_required"])

    def test_stale_hash_is_modified_not_trusted(self):
        # A hash written before the command changed must never read as trusted.
        def writer(real, blocks):
            out = ""
            for event, _ in blocks:
                out += '[hooks.state."%s:%s:0:0"]' % (real, LABEL[event]) + chr(10)
                out += "enabled = true" + chr(10)
                out += 'trusted_hash = "sha256:%s"' % ("a" * 64) + chr(10)
            return out
        rep = self.audit([("UserPromptSubmit", HOOK)], state_writer=writer)
        self.assertEqual(rep["status"], "awaiting_user_trust")
        self.assertEqual(rep["modified"], 1)
        self.assertEqual(rep["trusted"], 0)

    def test_disabled_entry(self):
        def writer(real, blocks):
            out = ""
            for event, command in blocks:
                out += '[hooks.state."%s:%s:0:0"]' % (real, LABEL[event]) + chr(10)
                out += "enabled = false" + chr(10)
                out += 'trusted_hash = "%s"' % current_hash(event, command) + chr(10)
            return out
        rep = self.audit([("Stop", HOOK)], state_writer=writer)
        self.assertEqual(rep["status"], "awaiting_user_trust")
        self.assertEqual(rep["disabled"], 1)

    def test_group_number_follows_positional_order(self):
        blocks = [("PreToolUse", OTHER), ("PreToolUse", HOOK)]

        def writer(real, b):
            # Only group 1 (the managed block) is trusted; group 0 stays out of audit.
            return ('[hooks.state."%s:pre_tool_use:1:0"]' % real + chr(10)
                    + "enabled = true" + chr(10)
                    + 'trusted_hash = "%s"' % current_hash("PreToolUse") + chr(10))
        rep = self.audit(blocks, state_writer=writer)
        self.assertEqual([b["group"] for b in rep["blocks"]], [1])
        self.assertTrue(rep["blocks"][0]["state_key"].endswith(":pre_tool_use:1:0"))
        self.assertEqual(rep["status"], "trusted")

    def test_partially_trusted(self):
        rep = self.audit([("UserPromptSubmit", HOOK), ("Stop", HOOK)],
                         trusted_events=["user_prompt_submit"])
        self.assertEqual(rep["status"], "partially_trusted")

    def test_timeout_normalization_matches_codex(self):
        # Absent timeout hashes as the Codex default (600); explicit 600 matches.
        self.assertEqual(current_hash("PreToolUse"), current_hash("PreToolUse", timeout=600))
        # SessionEnd default is 1, clamped to [1,3].
        self.assertEqual(current_hash("SessionEnd"), current_hash("SessionEnd", timeout=1))
        self.assertEqual(current_hash("SessionEnd", timeout=9), current_hash("SessionEnd", timeout=3))
        self.assertNotEqual(current_hash("SessionEnd"), current_hash("SessionEnd", timeout=3))

    def test_json_hooks_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "hooks.json"
            document = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": HOOK}]}]}}
            real.write_text(json.dumps(document), encoding="utf-8")
            rep = codex_trust.audit(real)
            self.assertEqual(rep["status"], "awaiting_user_trust")
            key = "%s:pre_tool_use:0:0" % real
            h = current_hash("PreToolUse")
            document["hooks"]["state"] = {key: {"enabled": True, "trusted_hash": h}}
            real.write_text(json.dumps(document), encoding="utf-8")
            rep = codex_trust.audit(real)
            self.assertEqual(rep["status"], "trusted")

    def test_not_installed_and_foreign_hooks_ignored(self):
        rep = self.audit([("PreToolUse", OTHER)])
        self.assertEqual(rep["status"], "not_installed")

    def test_malformed_config_reports_unreadable_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "config.toml"
            real.write_text("[[hooks.PreToolUse" + chr(10) + "broken = ", encoding="utf-8")
            rep = codex_trust.audit(real)
            self.assertEqual(rep["status"], "unreadable")

    def test_is_codex_excludes_lookalikes(self):
        self.assertTrue(codex_trust._is_codex({"identity": {"name": "Codex"}}))
        self.assertFalse(codex_trust._is_codex(
            {"identity": {"name": "OpenCodex", "entrypoint": "/opt/opencodex/dist/index.js"}}))
        self.assertTrue(codex_trust._is_codex({"identity": {"name": "ChatGPT",
            "entrypoint": "/Applications/ChatGPT.app/Contents/Resources/codex"}}))

    def test_live_config_hashes_reproduce(self):
        # Every existing trusted_hash on this machine must reproduce bit-exactly.
        live = Path.home() / ".codex" / "config.toml"
        if not live.is_file():
            self.skipTest("no live Codex config")
        import re
        src = live.read_text(encoding="utf-8", errors="ignore")
        events, state = codex_trust._parse_toml(src)
        verified = 0
        for event, groups in events.items():
            label = codex_trust.EVENT_KEY.get(event, event.lower())
            for gi, group in enumerate(groups):
                for hi, handler in enumerate(group["handlers"]):
                    key = "%s:%s:%d:%d" % (live, label, gi, hi)
                    entry = state.get(key)
                    if not entry or not entry.get("trusted_hash"):
                        continue
                    self.assertEqual(str(entry["trusted_hash"]),
                                     codex_trust.handler_hash(label, group.get("matcher"), handler),
                                     "hash mismatch for " + key)
                    verified += 1
        self.assertGreaterEqual(verified, 1)


if __name__ == "__main__":
    unittest.main()
