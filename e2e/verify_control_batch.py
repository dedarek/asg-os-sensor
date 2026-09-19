"""Task-3 acceptance batch: real Agent tool execution, independent effect check.

The driver only talks to the demo Agent HTTP API and the dashboard control
plane. It never creates a Hook, invokes a callback, or inserts events. Every
round is counted only when a fresh decision was actually delivered to the
Hook; a round without a delivered decision is recorded as such, never as a
pass. Effects are checked on the filesystem, not from an acknowledgement.

Usage:  python3 e2e/verify_control_batch.py [--allow 20] [--deny 20] [--concurrency 20]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import secrets
import statistics
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'artifacts/autonomous-demo'
DASHBOARD = 'http://127.0.0.1:8081'
WRITE_TOOLS = ('write', 'edit', 'bash')


def request(base, path, body=None, timeout=240):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'},
    )
    return json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout))


_POLICY_LOCK = threading.Lock()


def set_policy(decision, pid):
    with _POLICY_LOCK:
        return request(DASHBOARD, '/api/hook-control/policy', {
            'default': 'allow',
            'rules': [{'tool': t, 'decision': decision, 'pid': pid} for t in WRITE_TOOLS],
        })


def control_status():
    return request(DASHBOARD, '/api/hook-control/status')


def run_round(decision, meta, gateway, timeout=120):
    workspace = Path(meta['workspace'])
    marker = workspace / ('control-batch-' + secrets.token_hex(6) + '.txt')
    content = secrets.token_hex(16)
    start = time.time()
    session = request('http://127.0.0.1:' + str(meta['port']), '/session', {
        'title': 'ASG batch ' + decision,
        'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}],
    })
    request('http://127.0.0.1:' + str(meta['port']), '/session/' + session['id'] + '/message', {
        'model': {'providerID': 'demo', 'modelID': gateway['model']},
        'parts': [{'type': 'text', 'text': (
            'Use the write tool exactly once to create %s containing exactly %s. '
            'If the tool is denied, stop and report denial. Do not use another tool, '
            'shell, or retry.' % (marker, content))}],
    })
    deadline = start + timeout
    delivered = []
    while time.time() < deadline:
        events = control_status().get('events', [])
        delivered = [e for e in events if e.get('pid') == meta['pid']
                     and e.get('timestamp', 0) >= start
                     and e.get('event') == 'decision.returned'
                     and e.get('decision') == decision
                     and marker.name in json.dumps(e.get('input'), ensure_ascii=False)]
        if delivered:
            break
        time.sleep(1.5)
    if not delivered:
        return {'decision': decision, 'marker': str(marker), 'delivered': False,
                'effect_verified': False, 'session': session.get('id')}
    if decision == 'deny':
        ok = not marker.exists()
    else:
        ok = marker.exists() and marker.read_text().strip() == content
    return {'decision': decision, 'marker': str(marker), 'delivered': True,
            'executions': 1 if marker.exists() else 0, 'effect_verified': bool(ok),
            'session': session.get('id'),
            'decision_ids': [e['request_id'] for e in delivered]}


def decision_latencies(since):
    """Automatic decision round trip in ms, paired server-side by request id."""
    events = control_status().get('events', [])
    requested = {e['request_id']: e['timestamp'] for e in events
                 if e.get('event') == 'decision.requested' and e.get('timestamp', 0) >= since}
    out = []
    for e in events:
        if e.get('event') == 'decision.returned' and e.get('timestamp', 0) >= since:
            t0 = requested.get(e.get('request_id'))
            if t0 is not None:
                out.append((e['timestamp'] - t0) * 1000.0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--allow', type=int, default=20)
    ap.add_argument('--deny', type=int, default=20)
    ap.add_argument('--concurrency', type=int, default=20)
    args = ap.parse_args()

    meta = json.loads((DEMO / 'target.json').read_text())
    gateway = json.loads((DEMO / 'gateway.json').read_text())
    original = control_status()['policy']
    started = time.time()
    report = {'target': {'pid': meta['pid'], 'create_time': meta['create_time']},
              'mode': 'isolated_real_instance', 'started_at': started}
    try:
        set_policy('deny', meta['pid'])
        deny = [run_round('deny', meta, gateway) for _ in range(args.deny)]
        report['deny'] = {'requested': args.deny,
                          'delivered': sum(1 for r in deny if r['delivered']),
                          'effect_verified': sum(1 for r in deny if r['effect_verified']),
                          'no_effect': sum(1 for r in deny if r['delivered'] and not r['effect_verified']),
                          'no_decision': sum(1 for r in deny if not r['delivered']), 'rounds': deny}

        set_policy('allow', meta['pid'])
        allow = [run_round('allow', meta, gateway) for _ in range(args.allow)]
        report['allow'] = {'requested': args.allow,
                           'delivered': sum(1 for r in allow if r['delivered']),
                           'effect_verified': sum(1 for r in allow if r['effect_verified']),
                           'wrong_effect': sum(1 for r in allow if r['delivered'] and not r['effect_verified']),
                           'no_decision': sum(1 for r in allow if not r['delivered']),
                           'double_execution': sum(1 for r in allow if r['executions'] > 1), 'rounds': allow}

        # Concurrency: every call must map to its own decision and its own effect.
        set_policy('allow', meta['pid'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(20, args.concurrency)) as pool:
            concurrent_rounds = list(pool.map(lambda _: run_round('allow', meta, gateway), range(args.concurrency)))
        report['concurrency'] = {'requested': args.concurrency,
                                 'delivered': sum(1 for r in concurrent_rounds if r['delivered']),
                                 'effect_verified': sum(1 for r in concurrent_rounds if r['effect_verified']),
                                 'mapping_ok': sum(1 for r in concurrent_rounds
                                                  if r['delivered'] and len(r.get('decision_ids', [])) == 1
                                                  and r['effect_verified']),
                                 'rounds': concurrent_rounds}
    finally:
        request(DASHBOARD, '/api/hook-control/policy', original)

    lat = decision_latencies(started)
    if lat:
        srt = sorted(lat)
        report['decision_latency_ms'] = {'count': len(lat), 'p95': round(srt[min(len(srt) - 1, int(len(srt) * 0.95))], 1),
                                         'max': round(max(srt), 1), 'median': round(statistics.median(srt), 1)}
    report['finished_at'] = time.time()

    gate = {
        'allow_20_of_20': args.allow >= 20 and report['allow']['effect_verified'] == args.allow and report['allow']['delivered'] == args.allow,
        'deny_20_of_20': args.deny >= 20 and report['deny']['no_effect'] == 0 and report['deny']['delivered'] == args.deny,
        'no_double_execution': report['allow'].get('double_execution', 0) == 0,
        'concurrency_mapping_100pct': args.concurrency >= 20 and report['concurrency']['mapping_ok'] == args.concurrency,
        'auto_decision_p95_le_300ms': bool(report.get('decision_latency_ms')) and report['decision_latency_ms']['p95'] <= 300,
    }
    report['gates'] = gate
    report['passed'] = all(gate.values())
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'control-batch.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'report': str(path), 'gates': gate, 'passed': report['passed'],
                      'latency': report.get('decision_latency_ms')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
