"""Real HTTP acceptance, isolated fixtures; never writes into user Agent files."""
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path


class RegistrationHTTPTest(unittest.TestCase):
    def test_new_discovery_registers_once_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            def finding(folder,name,pid):
                path=root/folder/'investigation_findings.json';path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps({'target':{'pid':pid,'create_time':100},'findings':{'identity':{'status':'identified','value':{'name':name,'roles':['agent']}}}}))
                return path
            seed=finding('seed','Seed fixture',11)
            args=[sys.executable,'-B','-m','integrations.soc_inventory.preview','--source',str(seed),'--watch-root',str(root),'--port',str(port),'--interval','10','--db',str(root/'store.sqlite')]
            def get(endpoint):
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/'+endpoint,timeout=2) as response:return json.load(response)
            def until(predicate):
                end=time.monotonic()+20
                while time.monotonic()<end:
                    try:
                        value=predicate()
                        if value:return value
                    except OSError:pass
                    time.sleep(.1)
                self.fail('registration deadline exceeded')
            process=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                until(lambda:len(get('agent/status')['agents'])==1)
                finding('new','Previously unseen fixture',22)
                until(lambda:len(get('agent/status')['agents'])==2)
                platforms=get('admin/artifact-platforms')['platforms']
                matched=[p for p in platforms if p['name']=='Previously unseen fixture']
                self.assertEqual(len(matched),1)
                self.assertEqual(matched[0]['instance_count'],1)
                self.assertEqual(get('admin/artifacts/hooks/'+matched[0]['id'])['total'],0)
                token=get('admin/artifact-platforms')['merge_token']
                def merge(action,body,authorized=True):
                    request=urllib.request.Request(f'http://127.0.0.1:{port}/api/admin/artifact-platforms/'+action,data=json.dumps(body).encode(),headers={'Content-Type':'application/json', 'X-ASG-Preview-Token':token if authorized else ''})
                    with urllib.request.urlopen(request,timeout=3) as response:return json.load(response)
                source=matched[0]['id']
                with self.assertRaises(urllib.error.HTTPError) as denied:merge('merge',{'source':source,'target':'codex'},False)
                self.assertEqual(denied.exception.code,403)
                merge('merge',{'source':source,'target':'codex'})
                self.assertEqual(next(p for p in get('admin/artifact-platforms')['platforms'] if p['id']==source)['canonical_id'],'codex')
                merge('unmerge',{'source':source})
                self.assertEqual(next(p for p in get('admin/artifact-platforms')['platforms'] if p['id']==source)['canonical_id'],source)
                self.assertTrue(all(a['status']=='unknown' for a in get('agent/status')['agents']))
                finding('new','Previously unseen fixture',22)
                before=get('agent/status')['agents']
                process.terminate();process.wait(timeout=5)
                process=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                until(lambda:len(get('agent/status')['agents'])==2)
                self.assertEqual({a['id'] for a in before},{a['id'] for a in get('agent/status')['agents']})
            finally:
                process.terminate();process.wait(timeout=5)


if __name__=='__main__':unittest.main()
