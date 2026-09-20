"""Managed Codex hook blocks are trusted only via hooks.state entries."""
import tempfile, unittest
from pathlib import Path
from integrations.soc_inventory import codex_trust

HOOK = "/usr/bin/env python3 sec_hook.py security-hooks-codex"
OTHER = "/bin/sh /Users/test/.codex/asg-observer/run-hook.sh"


def cfg(path, blocks, state_keys):
    out = []
    for event, command in blocks:
        out.append("[[hooks.%s]]" % event)
        out.append("")
        out.append("[[hooks.%s.hooks]]" % event)
        out.append('type = "command"')
        out.append('command = "%s"' % command)
    for key in state_keys:
        out.append('[hooks.state."%s"]' % key)
        out.append("enabled = true")
        out.append('trusted_hash = "sha256:abc"')
    return chr(10).join(out) + chr(10)


class CodexTrustTests(unittest.TestCase):
    def audit(self, blocks, trusted_events=(), path="/tmp/x/config.toml"):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "config.toml"
            real.write_text(with_state(blocks, str(real), trusted_events), encoding="utf-8")
            return codex_trust.audit(real)

    def test_untrusted_without_state(self):
        rep = self.audit([("UserPromptSubmit", HOOK), ("PreToolUse", HOOK)])
        self.assertEqual(rep["status"], "awaiting_user_trust")
        self.assertEqual(rep["untrusted"], 2)
        self.assertTrue(rep["action_required"])

    def test_trusted_with_state(self):
        rep = self.audit([("UserPromptSubmit", HOOK), ("PreToolUse", HOOK)],
                         trusted_events=["user_prompt_submit", "pre_tool_use"])
        self.assertEqual(rep["status"], "trusted")
        self.assertFalse(rep["action_required"])

    def test_group_number_follows_positional_order(self):
        blocks = [("PreToolUse", OTHER), ("PreToolUse", HOOK)]
        rep = self.audit(blocks)
        self.assertEqual([b["group"] for b in rep["blocks"]], [1])
        self.assertTrue(rep["blocks"][0]["state_key"].endswith(":pre_tool_use:1:0"))

    def test_partially_trusted(self):
        rep = self.audit([("UserPromptSubmit", HOOK), ("Stop", HOOK)],
                         trusted_events=["user_prompt_submit"])
        self.assertEqual(rep["status"], "partially_trusted")

    def test_not_installed_and_foreign_hooks_ignored(self):
        rep = self.audit([("PreToolUse", OTHER)])
        self.assertEqual(rep["status"], "not_installed")

    def test_is_codex_excludes_lookalikes(self):
        self.assertTrue(codex_trust._is_codex({"identity": {"name": "Codex"}}))
        self.assertFalse(codex_trust._is_codex(
            {"identity": {"name": "OpenCodex", "entrypoint": "/opt/opencodex/dist/index.js"}}))
        self.assertTrue(codex_trust._is_codex({"identity": {"name": "ChatGPT",
            "entrypoint": "/Applications/ChatGPT.app/Contents/Resources/codex"}}))


if __name__ == "__main__":
    unittest.main()
def body(blocks):
    out = []
    for event, command in blocks:
        out.append("[[hooks.%s]]" % event)
        out.append("")
        out.append("[[hooks.%s.hooks]]" % event)
        out.append('type = "command"')
        out.append('command = "%s"' % command)
    return chr(10).join(out) + chr(10)


def with_state(blocks, path, trusted):
    text = body(blocks)
    for key in trusted:
        text += '[hooks.state."%s:%s:0:0"]' % (path, key) + chr(10)
        text += "enabled = true" + chr(10)
        text += 'trusted_hash = "sha256:abc"' + chr(10)
    return text
