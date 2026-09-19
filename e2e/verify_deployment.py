"""Clean-directory deployment smoke, including Unicode paths and idempotence."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile


def main():
    source=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='ASG 部署 test ') as tmp:
        root=Path(tmp).resolve()/'source'
        shutil.copytree(source,root,ignore=shutil.ignore_patterns('.git','.venv','.env','.tools','data','artifacts','__pycache__','deployment.local.json','*.log'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env={k:v for k,v in os.environ.items() if not k.startswith(('ASG_','GOOSE_'))};env['PYTHONUTF8']='1'
        def call(*args):
            r=subprocess.run([sys.executable,str(root/'deploy.py'),*args],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=300)
            if r.returncode: raise RuntimeError(r.stdout[-2000:]+'\n'+r.stderr[-2000:])
            return r.stdout
        call('setup','--skip-goose','--port',str(port),'--no-start')
        path=root/'deployment.local.json';config=json.loads(path.read_text(encoding='utf-8'))
        config['ASG_AUTONOMOUS_ANALYSIS']='0';config['ASG_ONBOARDING_AUTO_INSTALL']='0'
        path.write_text(json.dumps(config),encoding='utf-8')
        try:
            call('start');a=json.loads(call('status'))
            call('start');b=json.loads(call('status'))
            assert a['http_ready'] and b['http_ready'] and a['pid']==b['pid'],(a,b)
            assert all(a['dependencies'].values()),a
            call('stop');stopped=json.loads(call('status'));assert not stopped['running'],stopped
            call('start');c=json.loads(call('status'));assert c['running'] and c['http_ready'],c
            print(json.dumps({'passed':True,'platform':sys.platform,'python':sys.version.split()[0],
                  'gates':{'fresh_setup':True,'unicode_space_path':True,'dependencies':True,'http_ready':True,'idempotent_start':True,'stop':True,'restart':True}}))
        finally: call('stop')

if __name__=='__main__':main()
