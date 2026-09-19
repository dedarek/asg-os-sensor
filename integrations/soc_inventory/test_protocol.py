import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from .protocol import collect_contract, sha, canonical, workspace_identity
from .endpoint import parts_for, pack_skill, Endpoint

class ContractTest(unittest.TestCase):
    def test_codex_three_skills_two_mcp_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); home=root/'home';cwd=root/'repo';cwd.mkdir();(cwd/'.git').mkdir()
            for folder in [cwd/'.agents/skills/one',home/'.agents/skills/two',home/'.codex/skills/three']:
                folder.mkdir(parents=True);(folder/'SKILL.md').write_text('---\nname: '+folder.name+'\ndescription: Example\n---\nBody')
            (home/'.codex/config.toml').write_text('[mcp_servers.a]\ncommand="node"\nargs=["--token=SECRET"]\n[mcp_servers.b]\nurl="https://example.test/path?key=SECRET"\nenabled=false\n')
            agent={'agent_id':'test','platform':'codex','workspace':str(cwd)}
            report=collect_contract(agent,'epoch',1,home=home,env={})
            self.assertEqual(sum(len(s['items']) for s in report['categories']['skill']['scopes']),3)
            self.assertEqual(sum(len(s['items']) for s in report['categories']['mcp_server']['scopes']),2)
            self.assertNotIn(b'SECRET',canonical(report));self.assertNotIn('knowledge',report['categories'])
            parts,complete=parts_for(report)
            self.assertEqual(len(parts),json.loads(complete)['part_count'])
            self.assertTrue(all(sha(p)==h['sha256'] for p,h in zip(parts,json.loads(complete)['parts'])))
    def test_empty_and_parse_failure_differ(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'.hermes/skills').mkdir(parents=True)
            agent={'agent_id':'a','platform':'hermes','workspace':d}
            empty=collect_contract(agent,'e',1,root,{})
            self.assertEqual(empty['categories']['skill']['status'],'success')
            self.assertEqual(empty['categories']['skill']['scopes'][0]['items'],[])
            (root/'.hermes/config.yaml').write_text('mcp_servers: [broken')
            broken=collect_contract(agent,'e',2,root,{})
            self.assertEqual(broken['categories']['mcp_server']['status'],'failed')
    def test_archive_deterministic_complete_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'SKILL.md').write_text('---\nname: test\ndescription: test\n---')
            (root/'script.sh').write_text('echo test');(root/'script.sh').chmod(0o755)
            a,ha=pack_skill(root);b,hb=pack_skill(root)
            self.assertEqual((a,ha),(b,hb));self.assertEqual(set(zipfile.ZipFile(io.BytesIO(a)).namelist()),{'SKILL.md','script.sh'})
            (root/'link').symlink_to(root/'SKILL.md')
            with self.assertRaises(ValueError):pack_skill(root)
    def test_workspace_identity_and_missing_roots(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);repo=root/'repo';child=repo/'child';child.mkdir(parents=True);(repo/'.git').mkdir()
            agent={'agent_id':'a','platform':'codex','workspace':str(child)}
            self.assertEqual(workspace_identity(agent,root,{}),str(repo.resolve()))
            agent['workspace']=d
            self.assertEqual(workspace_identity(agent,root,{}),'nogit:'+str(root.resolve()))
            agent['platform']='hermes'
            self.assertEqual(workspace_identity(agent,root,{}),'hermes:'+str((root/'.hermes').resolve()))
            self.assertEqual(collect_contract(agent,'e',1,root,{})['categories']['skill']['scopes'],[])
    def test_offline_first_collection_survives_until_registration(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'.hermes/skills/example';folder.mkdir(parents=True)
            (folder/'SKILL.md').write_text('---\nname: original\ndescription: test\n---')
            agent={'agent_id':'a','platform':'hermes','workspace':d,'collection_home':d,'collection_environment':{}}
            cfg={'backend_url':'http://127.0.0.1:1','state_dir':str(root/'state'),'agents':[agent]}
            endpoint=Endpoint(cfg);endpoint.request=lambda *a: (_ for _ in ()).throw(OSError('offline'))
            with self.assertRaises(OSError):endpoint.enqueue(agent)
            self.assertEqual(endpoint.db.execute('SELECT count(*) FROM drafts').fetchone()[0],1)
            endpoint.db.close();(folder/'SKILL.md').write_text('changed after collection')
            resumed=Endpoint(cfg);resumed.request=lambda *a:{'collector_epoch':'real-epoch'}
            resumed.enqueue(agent)
            bodies=[json.loads(row[0]) for row in resumed.db.execute("SELECT body FROM queue WHERE path='/api/inventory'")]
            names=[item['name'] for body in bodies for scope in body['categories']['skill']['scopes'] for item in scope['items']]
            self.assertEqual(names,['original']);self.assertEqual(resumed.db.execute('SELECT count(*) FROM drafts').fetchone()[0],0)
    def test_bounded_reader_stops_slow_child(self):
        from unittest.mock import patch
        import time
        from .protocol import bounded_reader
        # Exercise the actual subprocess kill path without a slow filesystem.
        import subprocess
        real_run=subprocess.run
        def slow_run(command, **kwargs):
            import sys
            return real_run([sys.executable,'-c','import time;time.sleep(30)'],**kwargs)
        start=time.monotonic()
        with patch('integrations.soc_inventory.protocol.subprocess.run',side_effect=slow_run):
            with self.assertRaises(TimeoutError):bounded_reader('mcp',{},time.monotonic()+0.2)
        self.assertLess(time.monotonic()-start,3)
    def test_four_platform_empty_roots(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for platform,folder in [('codex','.codex'),('hermes','.hermes'),('opencode','.config/opencode'),('openclaw','.openclaw')]:
                (root/folder/'skills').mkdir(parents=True,exist_ok=True)
                if platform=='openclaw':(root/folder/'openclaw.json').write_text('{}')
                report=collect_contract({'agent_id':'a','platform':platform,'workspace':d},'e',1,root,{})
                self.assertEqual(report['categories']['skill']['status'],'success',platform)
    def test_learned_unknown_platform_and_removed_root(self):
        import shutil
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'learned'; root.mkdir()
            (root/'SKILL.md').write_text('---\nname: learned\ndescription: Test\n---')
            agent={'agent_id':'a','platform':'unknown-runtime','workspace':d,'learned_skill_roots':[str(root)]}
            found=collect_contract(agent,'e',1,home=d,env={})
            self.assertEqual(len(found['categories']['skill']['scopes'][0]['items']),1)
            shutil.rmtree(root)
            removed=collect_contract(agent,'e',2,home=d,env={},previous_scopes=['skill:'+str(root.resolve())])
            scope=removed['categories']['skill']['scopes'][0]
            self.assertEqual(scope['status'],'success');self.assertEqual(scope['items'],[])
    def test_once_fails_when_registration_failed(self):
        with tempfile.TemporaryDirectory() as d:
            endpoint=Endpoint({'backend_url':'http://127.0.0.1:1','state_dir':d,'agents':[{'agent_id':'a','platform':'codex','workspace':d}]})
            endpoint.request=lambda *args: (_ for _ in ()).throw(OSError('offline'))
            self.assertFalse(endpoint.run(True))
    def test_failed_upload_retains_exact_bytes_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            configuration={'backend_url':'http://127.0.0.1:1','state_dir':d,'agents':[{'agent_id':'a','platform':'codex','workspace':d,'key_file':str(Path(d)/'key')}]}
            endpoint=Endpoint(configuration)
            with endpoint.db:endpoint.db.execute('INSERT INTO queue(agent,path,method,body) VALUES(?,?,?,?)',('a','/api/inventory','POST',b'{"unchanged":true}'))
            endpoint.request=lambda *args: (_ for _ in ()).throw(OSError('offline'))
            endpoint.flush();endpoint.db.close()
            resumed=Endpoint(configuration)
            self.assertEqual(resumed.db.execute('SELECT body FROM queue').fetchone()[0],b'{"unchanged":true}')
            sent=[];resumed.request=lambda *args:sent.append(args[2]) or {}
            resumed.flush();self.assertEqual(sent,[b'{"unchanged":true}']);self.assertEqual(resumed.db.execute('SELECT count(*) FROM queue').fetchone()[0],0)

if __name__=='__main__':unittest.main()
