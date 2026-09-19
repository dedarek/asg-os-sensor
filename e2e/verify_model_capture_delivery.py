"""Verify actual Agent-facing model HTTP bodies, not native Hook coverage.

Requires the disposable demo gateway to have explicit capture enabled. This
driver invokes the real Agent API and never fabricates model or Hook events.
"""
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e.real_autonomous_demo import DEMO, request
from runtime.observation_registry import Registry


def response_text(body):
    if isinstance(body, dict):
        return ''.join(str(c.get('message', {}).get('content') or '') for c in body.get('choices', []))
    chunks = []
    for line in str(body).splitlines():
        if not line.startswith('data:') or line[5:].strip() == '[DONE]':
            continue
        data = json.loads(line[5:])
        chunks.extend(str(c.get('delta', {}).get('content') or '') for c in data.get('choices', []))
    return ''.join(chunks)


def main():
    target = json.loads((DEMO / 'target.json').read_text())
    gateway = json.loads((DEMO / 'gateway.json').read_text())
    log = DEMO / 'model-transport.jsonl'
    binding = DEMO / 'model-transport-binding.json'
    identity = {k: target[k] for k in ('pid', 'create_time')}
    binding.write_text(json.dumps({'version': 1, 'target': identity,
        'log_path': str(log), 'fields': {'event': 'event', 'pid': 'pid', 'timestamp': 'timestamp'},
        'event_names': {'model.request': 'model.request', 'model.response': 'model.response'}}))
    Registry(ROOT / 'artifacts/autonomous-service').register(binding, source_name='model-transport')
    canary = 'ASG-MODEL-TRANSPORT-' + uuid.uuid4().hex
    started = time.time()
    session = request(target['port'], 'POST', '/session', {'title': 'Model transport acceptance',
        'permission': [{'permission': '*', 'pattern': '*', 'action': 'deny'}]})
    reply = request(target['port'], 'POST', '/session/' + session['id'] + '/message', {
        'model': {'providerID': 'demo', 'modelID': gateway['model']},
        'parts': [{'type': 'text', 'text': 'Do not call tools. Reply with exactly this text: ' + canary}]}, timeout=240)
    (DEMO / 'model-transport-reply.json').write_text(json.dumps(reply, ensure_ascii=False, indent=2))
    groups = {}
    for line in log.read_text().splitlines():
        item = json.loads(line)
        if item.get('timestamp', 0) < started or item.get('pid') != identity['pid']:
            continue
        if item.get('create_time') != identity['create_time']:
            continue
        groups.setdefault(item['request_id'], {})[item['event']] = item
    found = []
    for rid, pair in groups.items():
        req, res = pair.get('model.request'), pair.get('model.response')
        if not req or not res:
            continue
        if not all(x.get('body_complete') is True and not x.get('truncated') for x in (req, res)):
            continue
        if canary in json.dumps(req['body']) and canary in response_text(res['body']):
            found.append(rid)
    assert found, 'No complete request/response pair containing the actual canary'
    assert canary in json.dumps(reply), 'Agent final response does not contain canary'
    report = {'target': identity, 'session_id': session['id'], 'verified_at': time.time(),
        'request_ids': found, 'canary': canary, 'request_and_response_body_verified': True,
        'agent_reply_verified': True, 'source': 'explicit_model_proxy',
        'scope': 'Agent-facing HTTP JSON/SSE bodies; TLS packets and provider-internal traffic excluded',
        'native_hook_network_capture': False, 'automatic_target_routing_verified': False,
        'setup': 'Explicit disposable test gateway configuration; connection ownership checked per request'}
    out = DEMO / 'model-transport-verification.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'report': str(out), 'complete_pairs': len(found), 'pid': identity['pid']}))


if __name__ == '__main__':
    main()
