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

    def test_fingerprint_import_routes_bundle_body_without_pid_suffix(self):
        # crazytest B20: the platform whitelists fingerprint_import; the bridge
        # must POST the bundle arguments to /api/recipe-bundle/import exactly
        # (no ?pid suffix -> the engine matches self.path verbatim) and turn a
        # 200 receipt into status completed.
        import inspect
        from . import runtime_bridge as rb
        self.assertEqual(rb.PATHS['fingerprint_import'],'/api/recipe-bundle/import')
        # The engine matches self.path verbatim for body-carrying routes, so
        # the pid-suffix guard inside run_once must keep this operation in its
        # no-pid set; otherwise dispatch would 404 and the receipt is stuck
        # 'uncertain' forever (four times in the 2026-09-23 platform records).
        src=inspect.getsource(rb.RuntimeBridge._runtime_cycle)
        guard=[line for line in src.splitlines()
               if "not in ('collect','scan','trust_refresh'" in line]
        self.assertEqual(len(guard),1)
        self.assertIn('fingerprint_import',guard[0])

if __name__=='__main__':unittest.main()
