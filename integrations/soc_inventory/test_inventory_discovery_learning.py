"""Learned investigation findings must feed the inventory contract (G09)."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from .discovery import first_mcp_field, learned_mcp_sources, confirmed


class McpLearning(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_json_and_nested_yaml_fields_are_located_by_structure(self):
        a = self.tmp / "a.json"
        a.write_text(json.dumps({"mcpServers": {"fs": {"command": "npx"}, "remote": {"url": "https://h/p", "type": "http"}}}))
        b = self.tmp / "b.yaml"
        b.write_text(os.linesep.join(["mcp:", "  servers:", "    t1:", "      command: run", ""]))
        out = learned_mcp_sources({"items": [{"name": "fs", "config_path": str(a)}, {"name": "t1", "source_path": str(b)}]})
        self.assertEqual({o["path"] for o in out}, {str(a.resolve()), str(b.resolve())})
        by = {o["path"]: o["field"] for o in out}
        self.assertEqual(by[str(a.resolve())], "mcpServers")
        self.assertEqual(by[str(b.resolve())], "mcp.servers")

    def test_non_declaration_paths_and_guesses_are_rejected(self):
        decoy = self.tmp / "c.json"
        decoy.write_text(json.dumps({"foo": {"bar": 1}}))
        out = learned_mcp_sources({"items": [
            {"name": "decoy", "source_path": str(decoy)},
            {"name": "binary", "source_path": "/bin/ls"},
            {"name": "ghost", "config_path": str(self.tmp / "missing.json")},
            {"name": "relative", "config_path": "config.json"},
            {"name": "runtime-only", "availability": "observed as child process pid 1"},
        ]})
        self.assertEqual(out, [])

    def test_first_field_deep_search(self):
        self.assertEqual(first_mcp_field({"tools": {"mcpServers": {"x": {"url": "https://h"}}}}), "tools.mcpServers")
        self.assertIsNone(first_mcp_field({"mcpServers": {"x": "not-a-definition"}}))

    def test_confirmed_feeds_learned_mcp_into_agent_record(self):
        cfg = self.tmp / "decl.json"
        cfg.write_text(json.dumps({"mcpServers": {"only": {"command": "srv"}}}))
        target = {"agents": [{"pid": 4242, "instance_id": "4242:123.5", "name": "learned-target",
            "identity": {"id": "unknown-platform", "version": "9"},
            "adapter": {"agent_classification": {"status": "confirmed_agent", "roles": ["agent"]},
                "assets": {"registered_tools_and_mcp": {"value": {"items": [{"name": "only", "config_path": str(cfg)}]}}}}}]}
        process = patch("psutil.Process").start().return_value
        process.create_time.return_value = 123.5
        process.cwd.return_value = "/workspace"
        process.environ.return_value = {}
        self.addCleanup(patch.stopall)
        agents = list(confirmed(target))
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["learned_mcp_configs"], [{"path": str(cfg.resolve()), "field": "mcpServers"}])

    def test_confirmed_preserves_evidence_backed_hook_workspace(self):
        target = {"agents": [{"pid": 4242, "instance_id": "4242:123.5", "name": "learned-target",
            "identity": {"id": "unknown-platform", "version": "9"},
            "adapter": {"agent_classification": {"status": "confirmed_agent", "roles": ["agent"]},
                "onboarding": {"plan": {"workspace": "/profiles/isolated", "fingerprint_id": "harness-one"}}}}]}
        process = patch("psutil.Process").start().return_value
        process.create_time.return_value = 123.5
        process.cwd.return_value = "/runtime/cwd"
        process.environ.return_value = {}
        self.addCleanup(patch.stopall)
        agent = list(confirmed(target))[0]
        self.assertEqual(agent["workspace"], "/runtime/cwd")
        self.assertEqual(agent["hook_workspace"], "/profiles/isolated")


if __name__ == "__main__":
    unittest.main()
