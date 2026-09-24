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
from integrations.soc_inventory.soc_onboarding import integrity,existing_install_mode
from integrations.soc_inventory.package_runtime.install import wire_model_request_payload,wire_soc_control_client,merge_structured_patch,pin_loader_revision,activation_affecting_files_changed,patch_migration_matches_prior

class DeploymentPackageTests(unittest.TestCase):
    def test_owned_patch_migration_accepts_only_the_superseded_observer(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'cordis.patch.yml'
            prior='''- insert:
  - id: asg-runtime-observer
    name: ./asg-runtime-observer/index.mjs
  - id: asg-observer
    name: ./plugins/asg-observer.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.mjs
  - id: administrator-hook
    name: ./admin.mjs
'''
            target.write_text('''- insert:
  - id: asg-observer
    name: ./plugins/asg-observer.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.mjs
  - id: administrator-hook
    name: ./admin.mjs
''')
            self.assertTrue(patch_migration_matches_prior('cordis.patch.yml',target,prior))
            target.write_text(target.read_text().replace('./admin.mjs','./other.mjs'))
            self.assertFalse(patch_migration_matches_prior('cordis.patch.yml',target,prior))
            self.assertFalse(patch_migration_matches_prior('other.yml',Path(tmp)/'missing.yml',prior))

    def test_transport_client_upgrade_does_not_require_agent_reload(self):
        prior={'plugins/asg-observer.mjs':'hook-v1',
               '.soc-hook/runtime/hook_control_client.mjs':'transport-v1'}
        transport_only={**prior,'.soc-hook/runtime/hook_control_client.mjs':'transport-v2'}
        hook_change={**transport_only,'plugins/asg-observer.mjs':'hook-v2'}
        self.assertFalse(activation_affecting_files_changed(prior,transport_only))
        self.assertTrue(activation_affecting_files_changed(prior,hook_change))

    def test_loader_revision_tracks_hook_and_replaces_owned_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);(workspace/'plugins').mkdir()
            target=workspace/'cordis.patch.yml'
            target.write_text('''- insert:\n  - id: keep-me\n    name: ./keep.mjs\n  - id: asg-observer\n    name: ./plugins/asg-observer.mjs\n''')
            plan={'version':1,'files':[
                {'path':'plugins/asg-observer.mjs','content':'new hook','expected_sha256':None},
                {'path':'cordis.patch.yml','content':'- insert:\n  - id: asg-observer\n    name: ./plugins/asg-observer.mjs\n','expected_sha256':None}]}
            merged=merge_structured_patch(pin_loader_revision(plan),workspace)
            patch=next(x for x in merged['files'] if x['path']=='cordis.patch.yml')
            import yaml
            entries=[entry for group in yaml.safe_load(patch['content']) for entry in group['insert']]
            by_id={entry['id']:entry for entry in entries}
            self.assertEqual(by_id['keep-me']['name'],'./keep.mjs')
            revision=hashlib.sha256(b'new hook').hexdigest()
            self.assertEqual(by_id['asg-observer']['config']['asg_package_revision'],revision)
            revisioned='plugins/asg-observer.'+revision+'.mjs'
            self.assertEqual(by_id['asg-observer']['name'],'./'+revisioned)
            copied=next(x for x in merged['files'] if x['path']==revisioned)
            self.assertEqual(copied['content'],'new hook')
            self.assertEqual(patch['expected_sha256'],hashlib.sha256(target.read_bytes()).hexdigest())

    def test_existing_profile_chooses_rebind_or_upgrade_from_exact_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);workspace=root/'profile';workspace.mkdir()
            state=root/('.asg-install-'+hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16])
            state.mkdir();package=root/'package';package.mkdir()
            (package/'recipe-bundle.json').write_text(json.dumps({'integrity':{'digest':'bundle-a'}}))
            (package/'transport.json').write_text(json.dumps({'installer_revision':'installer-a'}))
            receipt=state/'package-receipt.json'
            receipt.write_text(json.dumps({'bundle_digest':'bundle-a','installer_revision':'installer-a'}))
            agent={'workspace':str(workspace),'hook_workspace':str(workspace)}
            self.assertEqual(existing_install_mode(agent,package),'rebind')
            receipt.write_text(json.dumps({'bundle_digest':'bundle-old','installer_revision':'installer-a'}))
            self.assertEqual(existing_install_mode(agent,package),'upgrade')

    def test_reused_profile_keeps_existing_yaml_patch_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);target=workspace/'cordis.patch.yml'
            original="""# existing administrator patch
- insert:
    - id: existing-hook
      name: ./existing.mjs
"""
            target.write_text(original)
            plan={'version':1,'files':[{'path':'cordis.patch.yml','content':"""- insert:
    - id: asg-observer
      name: ./plugins/asg-observer.mjs
""",'expected_sha256':'0'*64}]}
            merged=merge_structured_patch(plan,workspace)
            content=merged['files'][0]['content']
            self.assertIn('existing-hook',content);self.assertIn('asg-observer',content)
            self.assertEqual(merged['files'][0]['expected_sha256'],hashlib.sha256(original.encode()).hexdigest())
            # Re-running on a file that already contains the same entry is
            # idempotent; a same-id/different-target collision is rejected.
            target.write_text(content)
            again=merge_structured_patch({'version':1,'files':[dict(plan['files'][0],content="""- insert:
    - id: asg-observer
      name: ./plugins/asg-observer.mjs
""")]},workspace)
            self.assertEqual(again['files'][0]['content'],content)
            with self.assertRaisesRegex(ValueError,'conflicting YAML patch id'):
                merge_structured_patch({'version':1,'files':[{'path':'cordis.patch.yml','content':"""- insert:
    - id: asg-observer
      name: ./other.mjs
""",'expected_sha256':None}]},workspace)

    def test_new_direct_hook_removes_legacy_asg_observer_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);target=workspace/'cordis.patch.yml'
            target.write_text("""- insert:
  - id: asg-runtime-observer
    name: ./asg-runtime-observer/index.mjs
    config:
      control:
        enabled: true
  - id: administrator-hook
    name: ./admin.mjs
    config:
      control:
        enabled: true
""")
            desired={'version':1,'files':[{'path':'cordis.patch.yml','content':"""- insert:
  - id: asg-observer
    name: ./plugins/asg-observer.mjs
""",'expected_sha256':None}]}
            content=merge_structured_patch(desired,workspace)['files'][0]['content']
            import yaml
            entries=[entry for group in yaml.safe_load(content) for entry in group['insert']]
            by_id={entry['id']:entry for entry in entries}
            # One agent, one observer: the legacy ASG duplicate is removed,
            # while unrelated administrator entries keep their own control.
            self.assertNotIn('asg-runtime-observer', by_id)
            self.assertTrue(by_id['administrator-hook']['config']['control']['enabled'])
            self.assertEqual(by_id['asg-observer']['name'],'./plugins/asg-observer.mjs')

    def test_model_request_capture_uses_complete_llm_stream_payload_and_chunks(self):
        source="""const bounded = (value) => ({ content: value, complete: true })
function apply (ctx) {
  // ── 2. 模型请求路由（waterfall：必须原样返回 next() 结果）──
  ctx.on('agent/request', async (payload, next) => {
    const resolved = await next()
    publish({
        event: 'model.request',
        content: {
          provider: (resolved && resolved.provider) ?? null,
          model: (resolved && resolved.model) ?? null,
          reasoningEffort: (resolved && resolved.reasoningEffort) ?? null,
        },
        content_complete: true,
    })
  })

  // ── 3. 工具执行前：同步 decision 门控 ──
}
"""
        wired,state=wire_model_request_payload(source)
        self.assertEqual(state,'injected')
        self.assertIn("ctx.on('llm/stream'",wired)
        self.assertIn("event: 'model.route'",wired)
        self.assertIn("event: index === 0 ? 'model.request' : 'model.request.chunk'",wired)
        self.assertIn("encoding: 'base64-json'",wired)
        self.assertIn('request_payload_complete: true',wired)
        self.assertNotIn('redactRequest(payload)',wired)
        self.assertIn("'[REDACTED]'",wired)
        again,state2=wire_model_request_payload(wired)
        self.assertEqual(state2,'already')
        self.assertEqual(wired,again)
        na,state3=wire_model_request_payload('const x=1')
        self.assertEqual(state3,'not_applicable')
        self.assertEqual(na,'const x=1')

    def test_invalid_yaml_recipe_is_rejected_before_publication(self):
        content="""client('event', record)
client('decision', request)
client('ack', { request_id: 'denied', applied: true, outcome: 'blocked' })
ctx.on('tools/pre-execute', async () => ({ kind: 'deny', reason: 'blocked' }))
// hook_control_client.py
"""
        bundle={'schema':'asg-recipe-bundle.v1','created_at':'test','fingerprint':{'id':'bad-yaml'},
                'recipe':{'install_plan':{'version':1,'files':[
                    {'path':'hook.mjs','content':content,'expected_sha256':None},
                    {'path':'cordis.patch.yml','content':'[]\n- insert:\n  - id: hook\n','expected_sha256':None}]}},
                'constraints':{'compatibility':{}},'verification':{}}
        bundle['integrity']={'algorithm':'sha256','digest':digest(bundle)}
        with self.assertRaisesRegex(ValueError,'invalid YAML'):
            build(bundle)

    def test_integrity_uses_same_profile_target_as_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cwd=root/'cwd';profile=root/'profile';cwd.mkdir();profile.mkdir()
            resolved=profile.resolve()
            state=resolved.parent/('.asg-install-'+hashlib.sha256(str(resolved).encode()).hexdigest()[:16])
            state.mkdir();installed=profile/'plugins/hook.mjs';installed.parent.mkdir();installed.write_text('verified')
            mutable=profile/'.soc-hook/artifacts/autonomous-service/hook-control-client.json'
            mutable.parent.mkdir(parents=True);mutable.write_text('{"policy":{"default":"deny"}}')
            plan_digest='a'*64
            (state/(plan_digest+'.json')).write_text(json.dumps({'status':'installed','changes':[
                {'path':'plugins/hook.mjs','after_sha256':hashlib.sha256(b'verified').hexdigest()},
                {'path':'.soc-hook/artifacts/autonomous-service/hook-control-client.json','after_sha256':'old-package-digest'}]}))
            (state/'package-receipt.json').write_text(json.dumps({'plan_digest':plan_digest}))
            agent={'workspace':str(cwd),'hook_workspace':str(profile)}
            self.assertTrue(integrity(None,agent,{'transport':'soc-direct-v1'}))

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
            self.assertIn(str(workspace/'.soc-hook/runtime/hook_control_client.mjs'),installed)
            self.assertIn('process.execPath',installed)
            self.assertIn(str(workspace/'.soc-hook/artifacts/autonomous-service/hook-control-client.json'),installed)
            self.assertNotIn('artifacts/stage1/dashboard/hook-control-client.json',installed)
            legacy=content.replace('process.execPath',json.dumps('/Library/old-machine/python3'))
            rewired=wire_soc_control_client(legacy,workspace/'.soc-hook')
            self.assertIn('process.execPath',rewired)
            self.assertNotIn('/Library/old-machine/python3',rewired)

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
            self.assertTrue((workspace/'.soc-hook/runtime/hook_control_client.mjs').is_file())
            args[args.index('--agent-id')+1]='different-agent'
            self.assertNotEqual(run().returncode,0)
            rebound=run(['--rebind'])
            self.assertEqual(rebound.returncode,0,rebound.stderr)
            self.assertEqual(json.loads(rebound.stdout)['status'],'installed')
            config=json.loads((workspace/'.soc-hook/artifacts/autonomous-service/hook-control-client.json').read_text())
            self.assertEqual(config['agent_id'],'different-agent')
            config['policy']={'default':'allow','rules':[{'tool':'write','decision':'deny'}]}
            (workspace/'.soc-hook/artifacts/autonomous-service/hook-control-client.json').write_text(json.dumps(config))
            # A second pass with the same binding is idempotent even though
            # create-only files now exist, a structured patch was merged, and
            # SOC has updated the package-created runtime policy file.
            repeated=run(['--rebind'])
            self.assertEqual(repeated.returncode,0,repeated.stderr)
            self.assertIn(json.loads(repeated.stdout)['status'],('installed','already_installed'))
            preserved=json.loads((workspace/'.soc-hook/artifacts/autonomous-service/hook-control-client.json').read_text())
            self.assertEqual(preserved['policy'],config['policy'])
            # Rebind is deliberately narrower than upgrade: a different
            # package or installer revision cannot use it to overwrite files.
            receipt=next(workspace.parent.glob('.asg-install-*/package-receipt.json'))
            record=json.loads(receipt.read_text());record['installer_revision']='0'*64
            receipt.write_text(json.dumps(record))
            self.assertNotEqual(run(['--rebind']).returncode,0)
            record['installer_revision']=json.loads((pkg/'transport.json').read_text())['installer_revision']
            receipt.write_text(json.dumps(record))
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

    def test_verified_package_can_upgrade_twice_without_restoring_ancient_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);workspace=root/'workspace';exe=Path(sys.executable).resolve()
            token=root/'agent.key';token.write_text('test-key')
            def make_package(marker):
                pkg=root/('package-'+marker);pkg.mkdir()
                hook='''import { appendFileSync } from "node:fs";\nconst CONTROL_CONFIG = "placeholder";\nconst LOG_PATH = "events.jsonl";\nfunction appendLine(path, text) { appendFileSync(path, text + "\\n"); }\nfunction redact(value, depth) { return value; }\nfunction record(event, meta) { const row = {event, meta, marker: "MARKER"}; appendLine(LOG_PATH, JSON.stringify(row)); }\n'''.replace('MARKER',marker)
                bundle={'schema':'asg-recipe-bundle.v1','created_at':'test','fingerprint':{'id':'upgrade'},
                        'recipe':{'install_plan':{'version':1,'files':[
                            {'path':'.hooks/callback.js','content':hook,'expected_sha256':None},
                            {'path':'cordis.patch.yml','content':'- insert:\n  - id: asg-observer\n    name: ./.hooks/callback.js\n','expected_sha256':None}]}},
                        'constraints':{'compatibility':{'platform':platform.system(),'architecture':platform.machine(),'runtime':'native','executable':hashlib.sha256(exe.read_bytes()).hexdigest()}},'verification':{}}
                bundle['integrity']={'algorithm':'sha256','digest':digest(bundle)}
                with tarfile.open(fileobj=io.BytesIO(build(bundle))) as tar:tar.extractall(pkg)
                return pkg
            packages=[make_package(x) for x in ('one','two','three')]
            base=['--target',str(workspace),'--exe',str(exe),'--backend-url','http://127.0.0.1:8095','--agent-id','upgrade-agent','--platform','upgrade-agent','--token-file',str(token)]
            def run(pkg,extra=()):return subprocess.run([sys.executable,str(pkg/'install.py')]+base+list(extra),capture_output=True,text=True)
            self.assertEqual(run(packages[0]).returncode,0)
            patch_inode=(workspace/'cordis.patch.yml').stat().st_ino
            second=run(packages[1],['--upgrade'])
            self.assertEqual(second.returncode,0,second.stderr)
            self.assertIn('cordis.patch.yml',json.loads(second.stdout)['activation_reload_signaled'])
            self.assertNotEqual((workspace/'cordis.patch.yml').stat().st_ino,patch_inode)
            third=run(packages[2],['--upgrade'])
            self.assertEqual(third.returncode,0,third.stderr)
            self.assertIn('three',(workspace/'.hooks/callback.js').read_text())
            (workspace/'.hooks/callback.js').write_text('user edit')
            self.assertNotEqual(run(packages[1],['--upgrade']).returncode,0)

if __name__=='__main__':unittest.main()
