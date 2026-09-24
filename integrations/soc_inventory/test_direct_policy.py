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

    def test_engine_http_4xx_receipts_as_failed_not_uncertain(self):
        # crazytest B21: a 404/400 from the engine proves the operation never
        # ran; recording 'uncertain' masked four deterministic route bugs as
        # ambiguous outcomes on 2026-09-23. HTTPError (an OSError subclass) must
        # be handled before the generic OSError arm, with failed for <500.
        import inspect
        from . import runtime_bridge as rb
        src=inspect.getsource(rb.RuntimeBridge._runtime_cycle)
        lines=src.splitlines()
        http=[i for i,l in enumerate(lines) if l.strip().startswith('except HTTPError as exc')]
        os_err=[i for i,l in enumerate(lines) if l.strip().startswith('except OSError as exc')]
        self.assertEqual(len(http),1)
        self.assertEqual(len(os_err),1)
        self.assertLess(http[0],os_err[0])
        arm='\n'.join(lines[http[0]:http[0]+14])
        self.assertIn("exc.code>=500",arm)
        self.assertIn("'failed'",arm)

if __name__=='__main__':unittest.main()
