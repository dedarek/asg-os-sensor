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
from runtime.recipe_bundle import digest, portable_recipe, resolve_bundle, scan_portability
from integrations.soc_inventory.deployment_package import build
from integrations.soc_inventory.package_runtime.install import wire_soc_control_client

class DeploymentPackageTests(unittest.TestCase):
    def test_goose_plugin_is_portable_and_uses_packaged_soc_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);workspace=root/'profile';source_root=root/'source'
            python='/machine/python3'
            plugin="""const clientCommand = [
  '/machine/python3',
  'SOURCE_ROOT/runtime/hook_control_client.py',
  'SOURCE_ROOT/artifacts/stage1/dashboard/hook-control-client.json',
]
const client = () => {}
client("event", record)
client("decision", request)
client("ack", { request_id: 'denied', applied: true, outcome: "blocked" })
ctx.on('tools/pre-execute', async () => ({ kind: "deny", reason: 'blocked' }))
""".replace('SOURCE_ROOT',str(source_root))
            recipe={'hook':{'workspace':str(workspace)},'observation_source':{
                'log_path':'plugins/events.jsonl','fields':{'event':'event','pid':'pid','timestamp':'timestamp'}},
                'install_plan':{'version':1,'files':[{'path':'plugins/hook.mjs','content':plugin,'expected_sha256':None}]}}
            portable,counts=portable_recipe(recipe,asg_root=str(source_root),target_workspace=str(workspace),python_executable=python)
            bundle={'schema':'asg-recipe-bundle.v1','created_at':'test','fingerprint':{'id':'goose'},'recipe':portable,
                    'constraints':{'compatibility':{'platform':platform.system(),'architecture':platform.machine(),
                    'runtime':'native','executable':hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()}},
                    'verification':{}}
            bundle['integrity']={'algorithm':'sha256','digest':digest(bundle)}
            self.assertEqual(set(counts),{'${TARGET_WORKSPACE}','${ASG_ROOT}','${PYTHON_EXECUTABLE}'})
            report=scan_portability(bundle,asg_root=str(source_root),target_workspace=str(workspace))
            self.assertEqual(report['machine_paths'],[])
            self.assertEqual(report['credential_hits'],[])
            self.assertIn('${PYTHON_EXECUTABLE}',report['placeholders'])
            archive=build(bundle)
            self.assertGreater(len(archive),1000)
            resolved=resolve_bundle(bundle,asg_root=workspace/'.soc-hook',target_workspace=workspace)
            content=resolved['recipe']['install_plan']['files'][0]['content']
            self.assertIn(sys.executable,content)
            self.assertIn(str(workspace/'.soc-hook/runtime/hook_control_client.py'),content)
            self.assertNotIn('/machine/python3',content)
            self.assertNotIn(str(source_root),content)
            installed=wire_soc_control_client(content,workspace/'.soc-hook')
            self.assertIn(str(workspace/'.soc-hook/artifacts/autonomous-service/hook-control-client.json'),installed)
            self.assertNotIn('artifacts/stage1/dashboard/hook-control-client.json',installed)

    def test_unproven_plugin_control_pattern_is_not_packaged(self):
        bundle={'schema':'asg-recipe-bundle.v1','created_at':'test','fingerprint':{'id':'bad'},
                'recipe':{'install_plan':{'version':1,'files':[{'path':'hook.mjs',
                'content':"client('event', row)", 'expected_sha256':None}]}},
                'constraints':{'compatibility':{}},'verification':{}}
        bundle['integrity']={'algorithm':'sha256','digest':digest(bundle)}
        with self.assertRaisesRegex(ValueError,'direct normalized event'):
            build(bundle)

    def test_plugin_without_denied_execution_ack_is_not_packaged(self):
        content="""client('event', record)
client('decision', request)
client('ack', { request_id: 'allowed', applied: true, outcome: 'allowed' })
ctx.on('tools/pre-execute', async () => ({ kind: 'deny', reason: 'blocked' }))
// hook_control_client.py
"""
        bundle={'schema':'asg-recipe-bundle.v1','created_at':'test','fingerprint':{'id':'missing-deny-ack'},
                'recipe':{'install_plan':{'version':1,'files':[{'path':'hook.mjs',
                'content':content,'expected_sha256':None}]}},
                'constraints':{'compatibility':{}},'verification':{}}
        bundle['integrity']={'algorithm':'sha256','digest':digest(bundle)}
        with self.assertRaisesRegex(ValueError,'direct normalized event'):
            build(bundle)

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

    def test_interpreter_entrypoint_is_pinned_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);pkg=root/'package';pkg.mkdir();workspace=root/'workspace'
            exe=Path(sys.executable).resolve();entry=root/'agent.py';entry.write_text('print("agent")\n')
            hook='''import { appendFileSync, readFileSync } from "node:fs";\nimport { createHash } from "node:crypto";\nconst CONTROL_CONFIG = "placeholder";\nconst LOG_PATH = "events.jsonl";\nfunction appendLine(path, text) { appendFileSync(path, text + "\\n"); }\nfunction redact(value, depth) { return value; }\nfunction record(event, meta) { const row = {event, meta};\n  appendLine(LOG_PATH, JSON.stringify(row));\n}\n'''
            compatibility={'platform':platform.system(),'architecture':platform.machine(),
                           'runtime':'python','executable':hashlib.sha256(exe.read_bytes()).hexdigest(),
                           'entry':hashlib.sha256(entry.read_bytes()).hexdigest(),
                           'entry_path':'/portable/original/agent.py','launch':'historical'}
            bundle={'schema':'asg-recipe-bundle.v1','created_at':'test','fingerprint':{'id':'script'},
                    'recipe':{'install_plan':{'version':1,'files':[{'path':'.hooks/callback.js',
                    'content':hook,'expected_sha256':None}]}},
                    'constraints':{'compatibility':compatibility},'verification':{}}
            bundle['integrity']={'algorithm':'sha256','digest':digest(bundle)}
            with tarfile.open(fileobj=io.BytesIO(build(bundle))) as tar:tar.extractall(pkg)
            token=root/'agent.key';token.write_text('isolated-test-key')
            args=[sys.executable,str(pkg/'install.py'),'--target',str(workspace),'--exe',str(exe),
                  '--entry',str(entry),'--platform','script','--backend-url','http://127.0.0.1:8095',
                  '--agent-id','script','--token-file',str(token),'--dry-run']
            ok=subprocess.run(args,capture_output=True,text=True)
            self.assertEqual(ok.returncode,0,ok.stderr)
            entry.write_text('print("changed")\n')
            bad=subprocess.run(args,capture_output=True,text=True)
            self.assertNotEqual(bad.returncode,0)
            self.assertIn('entrypoint build mismatch',bad.stderr)

if __name__=='__main__':unittest.main()
