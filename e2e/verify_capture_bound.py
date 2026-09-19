"""Verify the learned Hook retains full payloads (no silent/lossy truncation).

Sends a ~100 KB user message whose tail marker sits in the last bytes, and a
long assistant answer, then reads the Hook's own raw log and checks the marker
and the answer tail are present verbatim with no truncation flag.

Usage:  python3 e2e/verify_capture_bound.py
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'artifacts/autonomous-demo'
LOG = DEMO / 'workspace/.opencode/asg-logs/decision-hook-events.jsonl'


def request(base, path, body=None, timeout=300):
    req = urllib.request.Request(base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout))


def reply_text(response):
    parts = (response or {}).get('parts') or []
    return ''.join(str(p.get('text') or '') for p in parts if isinstance(p, dict) and p.get('type') == 'text')


def main():
    meta = json.loads((DEMO / 'target.json').read_text())
    gateway = json.loads((DEMO / 'gateway.json').read_text())
    target = 'http://127.0.0.1:' + str(meta['port'])
    session = request(target, '/session', {'title': 'capture-bound',
        'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}]})
    sid = session['id']
    tail = 'ASG-TAIL-' + secrets.token_hex(8)
    filler = ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(100 * 1024))
    user_text = ('以下是约 100 KB 的文本，请用中文回复“收到”，并原样包含结尾标记。\n'
                 + filler + '\n结束标记：' + tail)
    request(target, '/session/' + sid + '/message',
            {'model': {'providerID': 'demo', 'modelID': gateway['model']},
             'parts': [{'type': 'text', 'text': user_text}]}, timeout=300)
    answer = reply_text(request(target, '/session/' + sid + '/message',
        {'model': {'providerID': 'demo', 'modelID': gateway['model']},
         'parts': [{'type': 'text', 'text': '请用中文写一段不少于 8000 字符的说明，主题是 Hook 的信任与加载。'}]}, timeout=300))
    time.sleep(1.0)
    raw = LOG.read_text()
    lines = [l for l in raw.splitlines() if sid in l]
    user_lines = [l for l in lines if '"event": "user.input"' in l or '"event":"user.input"' in l]
    print(json.dumps({
        'session': sid,
        'tail_marker': tail,
        'user_message_bytes': len(user_text.encode()),
        'tail_marker_retained_verbatim': tail in raw,
        'user_input_line_has_old_truncation': any('"limit_bytes": 16384' in l for l in user_lines),
        'long_answer_chars': len(answer),
        'answer_tail_retained_verbatim': bool(answer) and answer[-40:] in raw,
        'session_lines': len(lines),
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
