"""Real installed Agent lifecycle driver; never writes or supplies a Hook.
Test model settings and stock dependencies are explicitly test infrastructure.
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
import psutil

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'artifacts' / 'autonomous-demo'
META = DEMO / 'target.json'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def request(port, method, path, body=None, timeout=30):
    req = urllib.request.Request('http://127.0.0.1:%s%s' % (port, path),
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json'}, method=method)
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)

def spawn_target(meta):
    run = Path(meta['run'])
    env = {k:v for k,v in os.environ.items() if not k.startswith(('ASG','OPENCODE','OPENAI','ANTHROPIC','GOOSE'))}
    env.update(HOME=str(run/'home'), XDG_CONFIG_HOME=str(run/'config'), XDG_CACHE_HOME=str(run/'cache'),
        XDG_DATA_HOME=str(run/'data'), XDG_STATE_HOME=str(run/'state'), OPENCODE_CONFIG_DIR=str(run/'config/opencode'))
    with (run/'target.log').open('ab') as log:
        process = subprocess.Popen([meta['cli'],'serve','--hostname','127.0.0.1','--port',str(meta['port'])],
            cwd=meta['workspace'], env=env, stdout=log, stderr=log, start_new_session=True)
    meta.update(pid=process.pid, create_time=psutil.Process(process.pid).create_time())
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    for _ in range(30):
        try:
            data=request(meta['port'],'GET','/path',timeout=2)
            if data.get('directory') == meta['workspace']:
                print(json.dumps({'pid':meta['pid'],'workspace':meta['workspace'],'ready':True})); return
        except Exception:
            time.sleep(.5)
    raise RuntimeError('real target did not become ready; see target.log')

def start():
    if META.exists():
        raise RuntimeError('Existing demo metadata; use restart or a separate DEMO directory')
    DEMO.mkdir(parents=True)
    for name in ('home','config/opencode','cache','data','state','workspace'):
        (DEMO/name).mkdir(parents=True)
    stock = Path('/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode')
    for target in (DEMO/'config/opencode', DEMO/'workspace/.opencode'):
        target.mkdir(exist_ok=True)
        shutil.copytree(stock/'node_modules', target/'node_modules')
        for name in ('package.json','package-lock.json','bun.lock','bun.lockb'):
            if (stock/name).is_file(): shutil.copy2(stock/name,target/name)
    (DEMO/'workspace/demo.txt').write_text('ASG real autonomous integration acceptance.\n')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    meta={'run':str(DEMO),'workspace':str(DEMO/'workspace'), 'port':port,
        'cli':'/Users/mac/.nvm/versions/node/v24.16.0/bin/opencode', 'hook_preinstalled':False,
        'dependencies':'stock SDK only', 'test_model_configuration':True}
    META.write_text(json.dumps(meta))
    with (DEMO/'gateway.log').open('ab') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()),'gateway'], cwd=ROOT,
            stdout=log, stderr=log, start_new_session=True)
    for _ in range(30):
        if (DEMO/'gateway.json').exists(): break
        time.sleep(.2)
    gateway=json.loads((DEMO/'gateway.json').read_text())
    model=gateway['model']
    config={'model':'demo/'+model,'provider':{'demo':{'npm':'@ai-sdk/openai-compatible',
        'name':'Isolated acceptance model','options':{'baseURL':gateway['base_url'],'apiKey':'local-proxy'},
        'models':{model:{'name':model,'tool_call':True,'limit':{'context':32768,'output':4096}}}}}}
    (DEMO/'config/opencode/opencode.json').write_text(json.dumps(config))
    spawn_target(meta)

def gateway():
    sys.path.insert(0,str(ROOT))
    settings=json.loads((ROOT/'artifacts/stage1/dashboard-assets-20260911/settings.json').read_text())
    for key in ('ASG_ANALYST_ENV_FILE','ASG_ANALYST_ROUTE'):
        os.environ[key]=settings[key]
    from runtime.llm_config import analyst_route, analyst_key
    from runtime.llm_proxy import ensure_proxy
    route=analyst_route(); key=analyst_key(route)
    if not key: raise RuntimeError('Authorized model credential unavailable')
    url=ensure_proxy(route,key)
    (DEMO/'gateway.json').write_text(json.dumps({'pid':os.getpid(),'base_url':url+'/v1','model':route['model']}))
    while True: time.sleep(30)

def restart():
    meta=json.loads(META.read_text())
    try:
        process=psutil.Process(meta['pid'])
        if abs(process.create_time()-meta['create_time'])>.001: raise RuntimeError('PID reused')
        # Only our disposable test instance. Never any desktop application.
        process.terminate(); process.wait(timeout=10)
    except psutil.NoSuchProcess: pass
    spawn_target(meta)

def ask():
    meta=json.loads(META.read_text()); gateway=json.loads((DEMO/'gateway.json').read_text())
    session=request(meta['port'],'POST','/session',{'title':'Read demo.txt',
        'permission':[{'permission':'*','pattern':'*','action':'deny'},
                      {'permission':'read','pattern':'*','action':'allow'}]})
    response=request(meta['port'],'POST','/session/'+session['id']+'/message',
        {'model':{'providerID':'demo','modelID':gateway['model']},'parts':[{'type':'text',
        'text':'Use the read tool to read demo.txt in this workspace, then report its contents. Do not modify files or execute shell commands.'}]},timeout=240)
    out=DEMO/('reply-%s-%s.json'%(meta['pid'],time.time_ns()))
    out.write_text(json.dumps(response,ensure_ascii=False,indent=2))
    print(json.dumps({'reply':str(out),'pid':meta['pid'],'session':session['id']}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['start','restart','ask','gateway']);args=parser.parse_args()
    globals()[args.action]()
