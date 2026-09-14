"""Exercise real Hook pause/resume through the dashboard approval API."""
import json
import secrets
import threading
import time
from pathlib import Path
from verify_control_delivery import request,DEMO

def main():
    meta=json.loads((DEMO/'target.json').read_text());gateway=json.loads((DEMO/'gateway.json').read_text())
    target='http://127.0.0.1:'+str(meta['port']);dashboard='http://127.0.0.1:8081'
    original=request(dashboard,'/api/hook-control/status')['policy'];errors=[];reply=[]
    marker=Path(meta['workspace'])/('confirmation-'+secrets.token_hex(6)+'.txt')
    session=request(target,'/session',{'title':'ASG confirmation acceptance','permission':[{'permission':'*','pattern':'*','action':'allow'}]})
    def invoke():
        try:reply.append(request(target,'/session/'+session['id']+'/message',{'model':{'providerID':'demo','modelID':gateway['model']},'parts':[{'type':'text','text':f'Use the write tool exactly once to create {marker} containing approval-test. If denied stop; do not retry or use any other tool.'}]}))
        except Exception as exc:errors.append(str(exc))
    thread=None
    try:
        request(dashboard,'/api/hook-control/policy',{'default':original['default'],'rules':[{'tool':'write','pid':meta['pid'],'decision':'ask'}]+original['rules']})
        thread=threading.Thread(target=invoke);thread.start();deadline=time.time()+180;pending=None
        while time.time()<deadline and thread.is_alive():
            pending=next((x for x in request(dashboard,'/api/hook-control/status')['pending'] if x['pid']==meta['pid'] and str(marker) in json.dumps(x,ensure_ascii=False)),None)
            if pending:break
            time.sleep(1)
        assert pending,'No real Hook confirmation request'
        assert not marker.exists(),'Operation ran before approval'
        request(dashboard,'/api/hook-control/resolve',{'request_id':pending['request_id'],'decision':'deny'})
        thread.join(180);assert not thread.is_alive() and not errors, str(errors)
        assert not marker.exists(),'Rejected confirmation still created file'
        result={'pid':meta['pid'],'session':session['id'],'request_id':pending['request_id'],'marker':str(marker),'paused_before_effect':True,'rejection_effect_verified':True}
        (DEMO/'confirmation-verification.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
    finally:request(dashboard,'/api/hook-control/policy',original)
if __name__=='__main__':main()
