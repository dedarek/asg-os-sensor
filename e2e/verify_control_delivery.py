"""Real disposable Agent acceptance; effects, not Hook acknowledgement, are proof.

This driver uses the demo Agent HTTP API only to exercise a learned Hook. It
never creates a Hook, invokes a callback directly, or inserts observation events.
"""
import json
import secrets
import sys
import time
import urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DEMO=ROOT/'artifacts/autonomous-demo'

def request(base,path,body=None,timeout=240):
    req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
    return json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=timeout))

def main():
    meta=json.loads((DEMO/'target.json').read_text());gateway=json.loads((DEMO/'gateway.json').read_text())
    target='http://127.0.0.1:'+str(meta['port']);dashboard='http://127.0.0.1:8081'
    original=request(dashboard,'/api/hook-control/status')['policy'];results=[]
    try:
        for decision in ('deny','allow'):
            marker=Path(meta['workspace'])/('control-acceptance-'+secrets.token_hex(6)+'.txt')
            content=secrets.token_hex(16)
            request(dashboard,'/api/hook-control/policy',{'default':original['default'],'rules':[{'tool':t,'decision':decision,'pid':meta['pid']} for t in ('write','edit','bash')]+original['rules']})
            start=time.time()
            session=request(target,'/session',{'title':'ASG Hook '+decision+' acceptance','permission':[{'permission':'*','pattern':'*','action':'allow'}]})
            reply=request(target,'/session/'+session['id']+'/message',{'model':{'providerID':'demo','modelID':gateway['model']},'parts':[{'type':'text','text':f'Use the write tool exactly once to create {marker} containing exactly {content}. If the tool is denied, stop and report denial. Do not use another tool, shell, or retry.'}]})
            events=request(dashboard,'/api/hook-control/status')['events']
            delivered=[x for x in events if x.get('pid')==meta['pid'] and x.get('timestamp',0)>=start and x.get('event')=='decision.returned' and x.get('decision')==decision]
            assert delivered, 'No fresh decision delivered by the real Hook'
            if decision=='deny':assert not marker.exists(),'Denied operation still created a file'
            else:assert marker.read_text().strip()==content,'Allowed operation did not create expected contents'
            results.append({'decision':decision,'pid':meta['pid'],'session':session['id'],'marker':str(marker),'decision_ids':[x['request_id'] for x in delivered],'effect_verified':True})
            (DEMO/('control-reply-'+decision+'.json')).write_text(json.dumps(reply,ensure_ascii=False))
    finally:request(dashboard,'/api/hook-control/policy',original)
    out=DEMO/'control-verification.json';out.write_text(json.dumps({'results':results,'source':'real Agent tool execution; independent filesystem check'},ensure_ascii=False,indent=2));print(json.dumps({'verified':True,'report':str(out),'results':results}))
if __name__=='__main__':main()
