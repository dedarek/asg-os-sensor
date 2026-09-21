"""A recipe bundle must be portable: no source-machine paths, receiver-resolved."""
import copy
import unittest

from runtime import recipe_bundle

DB = {"fingerprints": [{"id": "fp-port", "name": "Portable", "revision": 1,
                       "hook_recipe": {
                           "hook": {"method": "file_plan", "workspace": "/Users/dev/proj/ws"},
                           "install_plan": {"version": 1, "files": [
                               {"path": "h.js", "content": "const C='/Users/dev/proj/runtime/client.py';"
                                "const W='/Users/dev/proj/ws';", "expected_sha256": None}]}},
                       "revisions": [{"revision": 1, "compatibility": {"executable": "e", "platform": "Darwin"}}]}]}


class PortabilityTests(unittest.TestCase):
    def _bundle(self):
        return recipe_bundle.export_bundle("fp-port", db=copy.deepcopy(DB),
                                           asg_root="/Users/dev/proj", target_workspace="/Users/dev/proj/ws")

    def test_export_replaces_machine_paths_with_placeholders(self):
        bundle = self._bundle()
        scan = recipe_bundle.scan_portability(bundle, asg_root="/Users/dev/proj",
                                              target_workspace="/Users/dev/proj/ws")
        self.assertEqual(scan["machine_paths"], [])
        self.assertIn(recipe_bundle.PLACEHOLDER_ROOT, scan["placeholders"])
        self.assertIn(recipe_bundle.PLACEHOLDER_WORKSPACE, scan["placeholders"])
        self.assertIn("machine_parameters", bundle)

    def test_receiver_resolves_without_manual_edits(self):
        bundle = self._bundle()
        resolved = recipe_bundle.resolve_bundle(bundle, asg_root="/Users/other/machine",
                                                target_workspace="/Users/other/machine/ws")
        text = __import__("json").dumps(resolved, ensure_ascii=False)
        self.assertNotIn("/Users/dev/proj", text)
        self.assertIn("/Users/other/machine", text)
        # receiver transformation stays self-consistent for its own digest
        self.assertTrue(recipe_bundle.validate_bundle(resolved)["ok"])

    def test_scan_flags_credentials_and_chat(self):
        bundle = self._bundle()
        bundle["recipe"]["leaked"] = {"api_key": "x"}
        bundle["recipe"]["conversation"] = [{"role": "assistant", "content": "hi"}]
        scan = recipe_bundle.scan_portability(bundle)
        self.assertIn("api_key", scan["credential_hits"])
        self.assertIn("conversation", scan["chat_hits"])

    def test_export_from_another_checkout_rewrites_asg_owned_runtime_suffixes(self):
        db = copy.deepcopy(DB)
        recipe = db['fingerprints'][0]['hook_recipe']
        recipe['install_plan']['files'][0]['content'] = (
            "const clientCommand = ['/Library/Old/Python', "
            "'/Users/old/dev/asg/runtime/hook_control_client.py', "
            "'/Users/old/dev/asg/artifacts/stage1/dashboard/hook-control-client.json']")
        bundle = recipe_bundle.export_bundle('fp-port', db=db, asg_root='/opt/new/asg',
                                             target_workspace='/Users/dev/proj/ws')
        text = __import__('json').dumps(bundle['recipe'])
        self.assertNotIn('/Users/old/dev/asg', text)
        self.assertIn('${ASG_ROOT}/runtime/hook_control_client.py', text)
        self.assertIn('${ASG_ROOT}/artifacts/autonomous-service/hook-control-client.json', text)


if __name__ == "__main__":
    unittest.main()
