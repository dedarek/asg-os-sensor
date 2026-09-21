"""Installed Hook transport. Talks to SOC directly; no ASG service or imports."""
import json
import sys
import uuid
import hashlib
import os
import sqlite3
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
        event = {'event_id':event_id,'instance_id':instance,'event_type':event_type,'timestamp':data.get('timestamp'),'channel':'direct','payload':data}
        return deliver(config, event)
    if action == 'ack':
        # A host decision receipt is not proof that the tool actually executed.
        ack = {'event': 'execution.ack', 'request_id': str(data.get('request_id') or ''),
               'decision': str(data.get('decision') or ''), 'tool': data.get('tool'),
               'call_id': data.get('call_id'), 'outcome': data.get('outcome'),
               'timestamp': data.get('timestamp') or datetime.now(timezone.utc).isoformat()}
        for field in ('applied', 'executed'):
            if type(data.get(field)) is bool:
                ack[field] = data[field]
        result = exchange(config, ack, 'event')
        return {**result, 'scope': 'execution_ack_reported' if result.get('accepted')
                else 'execution_ack_queued', 'enforcement_verified': False}
    if action == 'flush':
        return deliver(config)
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


def deliver(config, event=None):
    """Persist before sending. Replays run on callbacks or explicit flush.

    No ASG imports or service. Concurrent callbacks may resend a batch after
    acknowledgement loss; stable event IDs let SOC deduplicate it. Delete only
    acknowledged rows, never a snapshot of the entire queue.
    """
    binding = [config['backend_url'], config['agent_id'],
               config.get('instance_id') or config['agent_id']]
    tag = hashlib.sha256(json.dumps(binding).encode()).hexdigest()[:24]
    folder = Path(config['token_file']).resolve().parent / 'soc-outbox'
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / (tag + '.sqlite3')
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(str(path), timeout=5)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        if event is not None:
            with db:
                db.execute('INSERT OR IGNORE INTO pending VALUES (?, ?)',
                           (event['event_id'], json.dumps(event, ensure_ascii=False)))
        sent = 0
        error = None
        rows = db.execute('SELECT id, body FROM pending ORDER BY rowid LIMIT 50').fetchall()
        if rows:
            try:
                # Telemetry must not consume the execution-decision timeout.
                cfg = {**config, 'timeout_seconds': min(float(config.get('timeout_seconds', 30)), 3)}
                reply = request(cfg, '/api/asg/events', {'instance_id': binding[2],
                                'events': [json.loads(row[1]) for row in rows]})
                if reply.get('accepted') is True:
                    with db:
                        db.executemany('DELETE FROM pending WHERE id=?', [(row[0],) for row in rows])
                    sent = len(rows)
                else:
                    error = 'soc_did_not_acknowledge'
            except (OSError, ValueError) as exc:
                error = type(exc).__name__
        pending = db.execute('SELECT COUNT(*) FROM pending').fetchone()[0]
        current_pending = event is not None and db.execute(
            'SELECT 1 FROM pending WHERE id=?', (event['event_id'],)).fetchone() is not None
        result = {'accepted': not current_pending if event is not None else pending == 0,
                  'queued': bool(current_pending if event is not None else pending),
                  'pending': pending, 'delivered': sent}
        if error:
            result['error'] = error
        return result
    finally:
        db.close()


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
