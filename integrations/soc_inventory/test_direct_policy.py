import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from .runtime_bridge import RuntimeBridge


class DirectPolicyTests(unittest.TestCase):
    def test_soc_policy_is_written_to_the_installed_hook_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'.soc-hook/artifacts/autonomous-service/hook-control-client.json'
            path.parent.mkdir(parents=True);path.write_text(json.dumps({'backend_url':'https://soc.example','agent_id':'a'}))
            bridge=object.__new__(RuntimeBridge)
            policy={'default':'allow','rules':[{'tool':'write','decision':'deny'}]}
            self.assertTrue(bridge.install_direct_policy({'hook_workspace':str(root)},policy))
            saved=json.loads(path.read_text())
            self.assertEqual(saved['policy'],policy)
            self.assertEqual(saved['backend_url'],'https://soc.example')


if __name__=='__main__':unittest.main()
