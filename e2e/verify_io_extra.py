"""Task-2 remaining scenarios: cancel, attachment, subagent — real Agent API.

Each round is driven by the real Agent HTTP API; the Hook's own log is scanned
for the corresponding capture. Unsupported capabilities are reported as such
with the observed reason, never silently dropped.

Usage:  python3 e2e/verify_io_extra.py [--rounds 5] [--attach 3]
"""
from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'artifacts/autonomous-demo'
LOG = DEMO / 'workspace/.opencode/asg-logs/decision-hook-events.jsonl'


def request(base, path, body=None, timeout=300, method='POST'):
    req = urllib.request.Request(base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'}, method=method)
    return json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout))


def new_session(target):
    return request(target, '/session', {'title': 'io-extra',
        'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}]})


def assistant_text(reply):
    if not isinstance(reply, dict):
        return ''
    return ''.join(str(p.get('text') or '') for p in (reply.get('parts') or [])
                   if isinstance(p, dict) and p.get('type') == 'text')


def round_cancel(target, gateway):
    session = new_session(target)
    sid = session['id']
    canary = 'ASG-CANCEL-' + secrets.token_hex(6)
    box = {}

    def send():
        try:
            box['reply'] = request(target, '/session/' + sid + '/message',
                {'model': {'providerID': 'demo', 'modelID': gateway['model']},
                 'parts': [{'type': 'text', 'text': '请用中文写一段不少于 8000 字符的说明 ' + canary}]}, timeout=300)
        except Exception as exc:  # noqa: BLE001
            box['error'] = type(exc).__name__ + ': ' + str(exc)[:160]

    thread = threading.Thread(target=send)
    thread.start()
    time.sleep(1.2)
    try:
        request(target, '/session/' + sid + '/abort', {})
        abort_sent = True
    except Exception:  # noqa: BLE001
        abort_sent = False
    thread.join(120)
    return {'session_id': sid, 'canary': canary, 'abort_sent': abort_sent,
            'assistant_text_len': len(assistant_text(box.get('reply'))), 'error': box.get('error')}


def round_attach(target, gateway, workspace):
    session = new_session(target)
    sid = session['id']
    name = 'asg-attach-' + secrets.token_hex(4) + '.txt'
    content = 'ASG-ATTACH-' + secrets.token_hex(6)
    path = Path(workspace) / name
    path.write_text(content + '\n')
    result = {'session_id': sid, 'attachment_name': name, 'attachment_marker': content}
    try:
        response = request(target, '/session/' + sid + '/message',
            {'model': {'providerID': 'demo', 'modelID': gateway['model']},
             'parts': [{'type': 'file', 'mime': 'text/plain', 'filename': name, 'url': path.as_uri()},
                       {'type': 'text', 'text': '请读取附件并原样回复其内容（包含其中的 ASG-ATTACH 标记）。'}]}, timeout=300)
        result['reply'] = assistant_text(response)
        result['ok'] = True
    except Exception as exc:  # noqa: BLE001
        result['ok'] = False
        result['error'] = type(exc).__name__ + ': ' + str(exc)[:200]
    return result


def round_subagent(target, gateway):
    session = new_session(target)
    sid = session['id']
    canary = 'ASG-SUB-' + secrets.token_hex(6)
    result = {'session_id': sid, 'canary': canary}
    try:
        response = request(target, '/session/' + sid + '/message',
            {'model': {'providerID': 'demo', 'modelID': gateway['model']},
             'parts': [{'type': 'subtask', 'prompt': '用一句话说明 1+1 等于几。', 'description': 'sub ' + canary,
                        'agent': 'build'},
                       {'type': 'text', 'text': '请把上面的子任务结果汇总回复。' + canary}]}, timeout=300)
        result['ok'] = True
        result['reply'] = assistant_text(response)
        children = request(target, '/session/' + sid + '/children', None, method='GET')
        result['children'] = [c.get('id') for c in children] if isinstance(children, list) else []
    except Exception as exc:  # noqa: BLE001
        result['ok'] = False
        result['children'] = []
        result['error'] = type(exc).__name__ + ': ' + str(exc)[:200]
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--attach', type=int, default=3)
    args = ap.parse_args()
    meta = json.loads((DEMO / 'target.json').read_text())
    gateway = json.loads((DEMO / 'gateway.json').read_text())
    target = 'http://127.0.0.1:' + str(meta['port'])
    workspace = meta['workspace']
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'io-extra.json'
    report = {'instance_id': '%s:%s' % (meta['pid'], meta['create_time']), 'started_at': time.time()}
    report['cancel'] = [round_cancel(target, gateway) for _ in range(args.rounds)]
    report['attach'] = [round_attach(target, gateway, workspace) for _ in range(args.attach)]
    report['subagent'] = [round_subagent(target, gateway) for _ in range(args.rounds)]
    time.sleep(1.0)
    by_session = {}
    for line in LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        det = rec.get('detail') if isinstance(rec.get('detail'), dict) else {}
        sid = det.get('session_id')
        if sid:
            by_session.setdefault(sid, []).append(rec)

    def blob(sid):
        return json.dumps(by_session.get(sid, []), ensure_ascii=False)

    for x in report['cancel']:
        recs = by_session.get(x['session_id'], [])
        x['captured'] = bool(recs)
        x['error_event_captured'] = any(r.get('event') == 'error' for r in recs)
    for x in report['attach']:
        x['reply_has_marker'] = x.get('attachment_marker', '') in (x.get('reply') or '')
        x['attachment_captured'] = bool(x.get('attachment_name')) and x['attachment_name'] in blob(x['session_id'])
        x['marker_captured'] = bool(x.get('attachment_marker')) and x['attachment_marker'] in blob(x['session_id'])
    for x in report['subagent']:
        x['children_captured'] = sum(1 for c in (x.get('children') or []) if by_session.get(c))

    gates = {
        'cancel_5_aborted_and_captured': len(report['cancel']) == args.rounds and all(
            y['abort_sent'] and y['captured'] and y['error_event_captured'] and y['assistant_text_len'] < 200
            for y in report['cancel']),
        'attach_3_delivered_and_captured': len(report['attach']) == args.attach and all(
            y.get('ok') and y.get('reply_has_marker') and y.get('attachment_captured') for y in report['attach']),
        'subagent_5_parent_child_captured': len(report['subagent']) == args.rounds and all(
            y.get('children') and y.get('children_captured', 0) >= 1 for y in report['subagent']),
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    report['finished_at'] = time.time()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'gates': gates, 'passed': report['passed']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
