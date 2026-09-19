import hashlib
import io
import json
import platform
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from runtime.recipe_bundle import digest
from integrations.soc_inventory.deployment_package import build

class DeploymentPackageTests(unittest.TestCase):
    def test_install_reinstall_conflict_rollback_build_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);pkg=root/'package';pkg.mkdir();workspace=root/'workspace'
            exe=Path(sys.executable).resolve()
            hook='''import { appendFileSync, readFileSync } from "node:fs";\nimport { createHash } from "node:crypto";\nconst CONTROL_CONFIG = "placeholder";\nconst LOG_PATH = "events.jsonl";\nfunction appendLine(path, text) { appendFileSync(path, text + "\\n"); }\nfunction redact(value, depth) { return value; }\nfunction record(event, meta) { const row = {event, meta};\n  appendLine(LOG_PATH, JSON.stringify(row));\n}\n'''
            bundle={'schema':'asg-recipe-bundle.v1','created_at':'test','fingerprint':{'id':'test'},'recipe':{'install_plan':{'version':1,'files':[{'path':'.hooks/callback.js','content':hook,'expected_sha256':None}]}},'constraints':{'compatibility':{'platform':platform.system(),'architecture':platform.machine(),'runtime':'native','executable':hashlib.sha256(exe.read_bytes()).hexdigest()}},'verification':{}}
            bundle['integrity']={'algorithm':'sha256','digest':digest(bundle)}
            archive=build(bundle)
            self.assertEqual(archive,build(bundle))
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:tar.extractall(pkg)
            token=root/'agent.key';token.write_text('isolated-test-key');token.chmod(0o600)
            args=['bash',str(pkg/'install/install.sh'),'--target',str(workspace),'--exe',str(exe),'--backend-url','http://127.0.0.1:8095','--agent-id','unit-agent','--platform','unit-agent','--token-file',str(token)]
            def run(extra=()):return subprocess.run(args+list(extra),capture_output=True,text=True)
            self.assertEqual(json.loads(run(['--dry-run']).stdout)['status'],'validated')
            self.assertFalse(workspace.exists())
            self.assertEqual(json.loads(run().stdout)['status'],'installed')
            self.assertEqual(json.loads(run().stdout)['status'],'already_installed')
            config=json.loads((workspace/'.soc-hook/artifacts/autonomous-service/hook-control-client.json').read_text())
            self.assertEqual(config['agent_id'],'unit-agent')
            self.assertEqual((workspace/'.soc-hook/agent.key').stat().st_mode & 0o777,0o600)
            self.assertTrue((workspace/'.soc-hook/runtime/hook_control_client.py').is_file())
            args[args.index('--agent-id')+1]='different-agent'
            self.assertNotEqual(run().returncode,0)
            args[args.index('--agent-id')+1]='unit-agent'
            file=workspace/'.hooks/callback.js';installed_content=file.read_text();file.write_text('user edit')
            self.assertNotEqual(run().returncode,0)
            self.assertNotEqual(run(['--uninstall']).returncode,0)
            self.assertEqual(file.read_text(),'user edit')
            file.write_text(installed_content)
            self.assertEqual(json.loads(run(['--uninstall']).stdout)['status'],'rolled_back')
            self.assertFalse(file.exists())
            wrong=root/'wrong-exe';wrong.write_bytes(b'not the expected executable')
            args[args.index('--exe')+1]=str(wrong)
            self.assertNotEqual(run().returncode,0)
            self.assertFalse(file.exists())

if __name__=='__main__':unittest.main()
