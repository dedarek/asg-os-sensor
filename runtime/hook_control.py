"""Local synchronous Hook decisions. Delivery is not proof of enforcement."""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
import psutil
from runtime.learned_install import _atomic
from runtime.analyst_evidence import _redact_text, _SECRET_KEY

_LOCK = threading.RLock()
_CHANGED = threading.Condition(_LOCK)
_PENDING = {}

def root():
    return Path(os.environ.get('ASG_RUN_DIR', 'artifacts/stage1/dashboard')).resolve()

def token_path():
    p=root()/'hook-control.token';p.parent.mkdir(parents=True,exist_ok=True)
    try:
        fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd,'w') as f:f.write(secrets.token_hex(32))
    return p

def contract():
    config={'base_url':'http://127.0.0.1:'+os.environ.get('ASG_PORT','8081')+'/api/hook-control','token_file':str(token_path())}
    config_path=root()/'hook-control-client.json'
    _atomic(config_path,json.dumps(config).encode())
    return {'version':1,'base_url':'http://127.0.0.1:'+os.environ.get('ASG_PORT','8081')+'/api/hook-control',
            'client_command':[sys.executable,str(Path(__file__).with_name('hook_control_client.py')),str(config_path),'decision'],
            'client_usage':'Send one JSON request on stdin; parse stdout. Client fills process create_time and authentication. Change final argument to ack for acknowledgement or event for a normalized observation event. Nonzero exit or decision other than allow means deny in control mode.',
            'request':{'pid':'actual Agent PID','create_time':'actual Agent process creation time (epoch seconds)',
                       'call_id':'unique call identifier','tool':'actual tool name','input':'actual input'},
            'protocol':'Read token_file at runtime; Authorization: Bearer <token>. POST /decision before execution; await response. decision=allow permits execution; deny prevents execution; ask waits for UI resolution up to timeout_s (1..60, default 30) then denies. Transport/error/malformed response must deny in control mode. After applying decision POST /ack {request_id, applied: true, outcome: allowed|blocked}. Send each normalized user/model/tool/session event using client action event; this local client durably spools it and a SOC package replaces the transport with direct authenticated delivery. Never claim an ack proves the action was prevented; verification must check actual effects.',
            'scope':'Use only if the evidenced target Hook can synchronously gate execution. Do not claim unsupported target callbacks can block.'}

def redact(v):
    if isinstance(v,dict):return {str(k):'[REDACTED]' if _SECRET_KEY.search(str(k)) or str(k).lower() in ('nonce','token') else redact(x) for k,x in v.items()}
    if isinstance(v,list):return [redact(x) for x in v]
    if isinstance(v,str):return _redact_text(v)
    return v

def policy():
    try:return json.loads((root()/'hook-control-policy.json').read_text())
    except FileNotFoundError:return {'default':'allow','rules':[]}

def set_policy(value):
    if value.get('default') not in ('allow','deny','ask'):raise ValueError('default must be allow/deny/ask')
    rules=value.get('rules',[])
    if not isinstance(rules,list) or len(rules)>100:raise ValueError('rules must be a list of at most 100 rules')
    for r in rules:
        if not isinstance(r,dict) or not isinstance(r.get('tool'),str) or not r['tool'] or r.get('decision') not in ('allow','deny','ask'):raise ValueError('each rule requires exact tool and decision')
        if r.get('pid') is not None and (not isinstance(r['pid'],int) or r['pid']<=0):raise ValueError('rule pid must be positive integer')
    result={'default':value['default'],'rules':[{k:r[k] for k in ('tool','decision','pid') if k in r} for r in rules]}
    with _LOCK:_atomic(root()/'hook-control-policy.json',json.dumps(result).encode())
    return result

def _log(event):
    p=root()/'hook-control-events.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
    with _LOCK:
        with p.open('a') as f:f.write(json.dumps(redact({'timestamp':time.time(),**event}),ensure_ascii=False)+'\n')

def _live(data):
    pid=int(data['pid']);ct=float(data['create_time'])
    if abs(psutil.Process(pid).create_time()-ct)>=.001:raise ValueError('target instance changed')
    registry=root()/'observations.json'
    bindings=json.loads(registry.read_text()) if registry.exists() else {}
    if not any(x.get('target',{}).get('pid')==pid and abs(float(x.get('target',{}).get('create_time',0))-ct)<.001 for x in bindings.values()):raise ValueError('target has no observation binding')
    return {'pid':pid,'create_time':ct}

def decide(data):
    target=_live(data)
    if not isinstance(data.get('tool'),str) or not data['tool'] or not isinstance(data.get('call_id'),str) or not data['call_id']:raise ValueError('tool and call_id required')
    p=policy();decision=next((x['decision'] for x in p['rules'] if x['tool']==data['tool'] and (x.get('pid') is None or x['pid']==target['pid'])),p['default'])
    rid=secrets.token_hex(16)
    record={'request_id':rid,**target,'tool':data['tool'],'call_id':data['call_id'],'input':redact(data.get('input')),'decision':decision,'status':'pending' if decision=='ask' else 'decided'}
    timeout=max(1,min(60,float(data.get('timeout_s',30))))
    with _CHANGED:
        _PENDING[rid]=record
        _log({'event':'decision.requested',**record})
        deadline=time.monotonic()+timeout
        while record['decision']=='ask':
            remaining=deadline-time.monotonic()
            if remaining<=0:
                record.update(decision='deny',reason='confirmation_timeout');break
            _CHANGED.wait(remaining)
        _live(data)
        record['status']='decided'
        _log({'event':'decision.returned',**record})
        # Keep only bounded finished history in memory; pending calls are retained.
        for key in list(_PENDING):
            if len(_PENDING)<=500:break
            if _PENDING[key]['status']!='pending':_PENDING.pop(key)
        return {'request_id':rid,'decision':record['decision'],'reason':record.get('reason','policy'),'enforcement_verified':False}

def resolve(data):
    if data.get('decision') not in ('allow','deny'):raise ValueError('decision must be allow or deny')
    with _CHANGED:
        r=_PENDING.get(data.get('request_id'))
        if not r or r['status']!='pending' or r['decision']!='ask':raise ValueError('request is no longer pending')
        r.update(decision=data['decision'],reason='user_confirmation');_CHANGED.notify_all()
    return {'status':'resolved'}

def ack(data):
    with _LOCK:
        r=_PENDING.get(data.get('request_id'))
        if not r or r['status']!='decided':raise ValueError('unknown or unfinished decision')
        expected='blocked' if r['decision']=='deny' else 'allowed'
        if data.get('applied') is not True or data.get('outcome')!=expected:raise ValueError('ack does not match returned decision')
        r['status']='producer_acknowledged'
        _log({'event':'decision.acknowledged',**r,'outcome':expected,'enforcement_verified':False})
    return {'status':'producer_acknowledged','enforcement_verified':False}

def remote_enabled():
    """Opt-in flag for serving decisions to a remote caller."""
    return os.environ.get('ASG_HOOK_REMOTE','').strip().lower() in ('1','true','yes','on')


def authorized(handler):
    """Loopback is always allowed; a remote caller needs the shared token.

    Enabling remote access does not weaken the credential: the same bearer
    token the generated Hooks use is required, and cross-origin browser
    requests are still refused.  The returned reason keeps a 403 explainable
    in the UI instead of looking like a generic failure.
    """
    host=handler.client_address[0]
    if host in ('127.0.0.1','::1'):
        return True,None
    if not remote_enabled():
        return False,'loopback only (set ASG_HOOK_REMOTE=1 to allow authenticated remote callers)'
    expected='Bearer '+token_path().read_text().strip()
    if hmac.compare_digest(handler.headers.get('Authorization',''),expected):
        return True,None
    return False,'bearer token required for remote access'


def status():
    path=root()/'hook-control-events.jsonl';events=[]
    if path.exists():
        with path.open('rb') as f:
            f.seek(max(0,path.stat().st_size-1024*1024));raw=f.read().decode(errors='replace')
        for line in raw.splitlines()[-100:]:
            try:events.append(json.loads(line))
            except ValueError:pass
    with _LOCK:pending=[dict(x) for x in _PENDING.values() if x['status']=='pending' and x['decision']=='ask']
    from runtime.hook_acceptance import snapshots
    return {'policy':policy(),'pending':pending,'events':events,'verifications':snapshots(),'enforcement_verified':False}

def handle_request(handler):
    from urllib.parse import urlparse
    path=urlparse(handler.path).path
    if not path.startswith('/api/hook-control/'):return False
    try:
        allowed,reason=authorized(handler)
        if not allowed:raise PermissionError(reason)
        origin=handler.headers.get('Origin')
        if origin and urlparse(origin).netloc!=handler.headers.get('Host'):raise PermissionError('cross-origin request denied')
        if handler.command=='GET' and path.endswith('/status'):
            result=status()
        elif handler.command=='POST':
            length=int(handler.headers.get('Content-Length','0'))
            if not 0<length<=1024*1024:raise ValueError('request must be 1..1048576 bytes')
            data=json.loads(handler.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('object required')
            action=path.rsplit('/',1)[-1]
            if action in ('decision','ack'):
                expected='Bearer '+token_path().read_text().strip()
                if not hmac.compare_digest(handler.headers.get('Authorization',''),expected):raise PermissionError('Hook credential required')
            result={'decision':decide,'ack':ack,'policy':set_policy,'resolve':resolve}[action](data)
        else:raise ValueError('unknown endpoint')
        code=200
    except PermissionError as exc:code,result=403,{'error':str(exc)}
    except (ValueError,KeyError,TypeError,OSError,psutil.Error) as exc:code,result=400,{'error':str(exc)}
    handler.send_response(code);handler.send_header('Content-Type','application/json; charset=utf-8');handler.send_header('Cache-Control','no-store');handler.end_headers()
    handler.wfile.write(json.dumps(result,ensure_ascii=False).encode());return True
