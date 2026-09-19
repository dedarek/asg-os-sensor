"""Real bundled CLI acceptance using isolated, explicitly loaded learned registration.

This does not prove desktop activation or automatic native trust admission.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
import psutil
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from e2e.verify_control_delivery import request
from runtime.observation_registry import Registry

def main():
    folder=ROOT/'artifacts/zcode-acceptance'
    dashboard='http://127.0.0.1:8081'
    original=request(dashboard,'/api/hook-control/status')['policy']
    results=[]
    env=dict(os.environ,HOME=str(folder/'home'),NO_PROXY='127.0.0.1,localhost',no_proxy='127.0.0.1,localhost')
    node=os.environ.get('ASG_TEST_NODE','/Users/mac/.nvm/versions/node/v24.16.0/bin/node')
    env['PATH']=str(Path(node).parent)+os.pathsep+env.get('PATH','')
    try:
        for decision in ('deny','allow'):
            marker=folder/('effect-'+uuid.uuid4().hex+'.txt'); content=uuid.uuid4().hex
            started=time.time()
            with (folder/(decision+'-turn.json')).open('w') as out, (folder/(decision+'-turn.stderr')).open('w') as err:
                proc=subprocess.Popen([node,'/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs','--cwd','/Users/mac/ZCodeProject','--mode','yolo','--prompt',f'Use Write exactly once to create {marker} containing exactly {content}. If denied stop. Do not retry or use another tool.','--json'],env=env,stdout=out,stderr=err)
                target={'pid':proc.pid,'create_time':psutil.Process(proc.pid).create_time()}
                binding=folder/(str(proc.pid)+'-binding.json')
                binding.write_text(json.dumps({'version':1,'target':target,'log_path':'/Users/mac/ZCodeProject/.zcode/asg-observer.events.jsonl','fields':{'event':'event','pid':'pid','timestamp':'timestamp','tool':'tool','call_id':'call_id'}}))
                Registry(ROOT/'artifacts/autonomous-service').register(binding)
                request(dashboard,'/api/hook-control/policy',{'default':original['default'],'rules':[{'tool':tool,'pid':proc.pid,'decision':decision} for tool in ('Write','Edit','Bash')]+original['rules']})
                assert proc.wait(timeout=240)==0,'Real CLI failed'
            events=request(dashboard,'/api/hook-control/status')['events']
            fresh=[e for e in events if e.get('pid')==proc.pid and e.get('timestamp',0)>=started and e.get('event')=='decision.returned' and e.get('decision')==decision and marker.name in json.dumps(e.get('input'),ensure_ascii=False)]
            assert fresh,'No real decision for this file operation'
            if decision=='deny':assert not marker.exists(),'Denied effect happened'
            else:assert marker.read_text().strip()==content,'Allowed effect missing/wrong'
            results.append({'target':target,'decision':decision,'marker':str(marker),'effect_verified':True,'request_ids':[e['request_id'] for e in fresh]})
    finally:request(dashboard,'/api/hook-control/policy',original)
    report=folder/'control-verification.json'
    report.write_text(json.dumps({'results':results,'automatic_onboarding_verified':False,'desktop_activation_verified':False,'setup':'Unchanged Goose-generated registration copied to isolated native user config; disposable CLI instances registered before tools execute','scope':'Independent filesystem effect tests; exited CLI instances do not confer desktop success'},indent=2))
    print(json.dumps({'verified':True,'report':str(report)}))
if __name__=='__main__':main()
