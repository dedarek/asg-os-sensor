"""Installed Hook transport. Talks to SOC directly; no ASG service or imports."""
import json
import sys
import uuid
import hashlib
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlsplit
from datetime import datetime, timezone


def exchange(config, data, action):
    if action == 'event':
        event_type=str(data.get('event') or data.get('event_type') or '')
        if not event_type or len(event_type)>200:raise ValueError('invalid event type')
        instance=str(config.get('instance_id') or config['agent_id'])
        event_id=hashlib.sha256(json.dumps([instance,data],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return request(config,'/api/asg/events',{'instance_id':instance,'events':[{'event_id':event_id,'instance_id':instance,'event_type':event_type,'timestamp':data.get('timestamp'),'payload':data}]})
    if action == 'ack':
        # This is a producer acknowledgement, not independent enforcement proof.
        return {'accepted': True, 'scope': 'local_execution_ack', 'enforcement_verified': False}
    if action != 'decision':
        raise ValueError('unsupported Hook action')
    payload={'platform':config['platform'],'agent_id':config['agent_id'],'agent_name':config.get('agent_name',''),
             'layer':'before_tool_call','session_id':str(data.get('session_id') or data.get('pid') or 'default'),
             'timestamp':datetime.now(timezone.utc).isoformat(),'query':json.dumps(data.get('input'),ensure_ascii=False),
             'tool':{'name':data.get('tool'),'call_id':data.get('call_id'),'args':data.get('input')},
             'extra':{'protocol_version':'1.0','pid':data.get('pid')},'trace':[]}
    raw=request(config,'/api/analyze/before-tool-call',payload)
    nested=raw.get('ret_data') or {}
    action=raw.get('action',nested.get('action'))
    if action in (0,'0','allow','audit','inject'):decision='allow'
    elif action in (3,9,8,'3','9','8','block','confirm'):decision='deny'
    else:raise ValueError('SOC returned no recognized decision')
    return {'request_id':raw.get('request_id') or str(uuid.uuid4()),'decision':decision,
            'reason':raw.get('message') or raw.get('ret_msg') or 'SOC policy','enforcement_verified':False}


def request(config,path,payload):
    base=config['backend_url'].rstrip('/')
    url=urlsplit(base)
    if url.scheme!='https' and not (url.scheme=='http' and url.hostname in ('127.0.0.1','localhost','::1')):
        raise ValueError('remote SOC requires HTTPS')
    token=Path(config['token_file']).read_text().strip()
    req=Request(base+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    opener=build_opener(ProxyHandler({})) if url.hostname in ('127.0.0.1','localhost','::1') else build_opener()
    with opener.open(req,timeout=config.get('timeout_seconds',30)) as response:return json.load(response)


def main():
    config=json.loads(Path(sys.argv[1]).read_text())
    print(json.dumps(exchange(config,json.load(sys.stdin),sys.argv[2] if len(sys.argv)>2 else 'decision')))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'decision':'deny','reason':'soc_unavailable','error':type(exc).__name__,'enforcement_verified':False}));sys.exit(1)
