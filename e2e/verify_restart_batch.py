"""Task-4 acceptance: after each real restart, prove the new instance is
observable and controllable from fresh evidence, with no manual config edit.

Each cycle restarts the disposable target, waits for the supervisor to
auto-bind the new pid/create_time, then runs real chat + real allow/deny
effects and checks the new instance's own records. A restarted instance that
only inherits the old acceptance is never counted as a pass.

Usage:  python3 e2e/verify_restart_batch.py [--rounds 5]
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'artifacts/autonomous-demo'
DASH = 'http://127.0.0.1:8081'
RUN = ROOT / 'artifacts/autonomous-service'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(base, path, body=None, timeout=240):
    req = urllib.request.Request(base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'})
    return json.load(OPENER.open(req, timeout=timeout))


def meta():
    return json.loads((DEMO / 'target.json').read_text())


def iid_of(m):
    return '%s:%s' % (m['pid'], m['create_time'])


def bound(iid):
    for candidate in (RUN / 'observations.json',
                      ROOT / 'artifacts/stage1/dashboard/observations.json'):
        try:
            registry = json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(registry, dict) and iid in registry:
            return True
    return False


def restart_target():
    subprocess.run(['python3', 'e2e/real_autonomous_demo.py', 'restart'], cwd=str(ROOT),
                   capture_output=True, text=True, timeout=60)


def reply_text(response):
    return ''.join(str(p.get('text') or '') for p in ((response or {}).get('parts') or [])
                   if isinstance(p, dict) and p.get('type') == 'text')


def chat_round(target, gateway, sid, canary):
    started = time.time()
    request(target, '/session/' + sid + '/message',
            {'model': {'providerID': 'demo', 'modelID': gateway['model']},
             'parts': [{'type': 'text', 'text': '请用中文简短确认收到 ' + canary}]}, timeout=240)
    return started


def wait_captured(iid, canary, deadline=10.0):
    import urllib.parse
    label = None
    end = time.time() + deadline + 5
    while time.time() < end:
        q = urllib.parse.urlencode({'instance_id': iid, 'limit': 200})
        try:
            d = json.load(urllib.request.urlopen(DASH + '/api/hook-data?' + q, timeout=15))
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
            continue
        blob = json.dumps(d.get('records') or [], ensure_ascii=False)
        if canary in blob:
            return True
        time.sleep(0.5)
    return False


def control_round(target, gateway, pid, decision):
    marker = Path(meta()['workspace']) / ('restart-' + decision + '-' + secrets.token_hex(6) + '.txt')
    content = secrets.token_hex(16)
    request(DASH, '/api/hook-control/policy',
            {'default': 'allow', 'rules': [{'tool': t, 'decision': decision, 'pid': pid}
                                           for t in ('write', 'edit', 'bash')]})
    session = request(target, '/session', {'title': 'restart ' + decision,
        'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}]})
    request(target, '/session/' + session['id'] + '/message',
            {'model': {'providerID': 'demo', 'modelID': gateway['model']},
             'parts': [{'type': 'text', 'text': ('Use the write tool exactly once to create %s containing %s. '
                       'If denied stop.' % (marker, content))}]}, timeout=240)
    if decision == 'deny':
        return not marker.exists()
    return marker.exists() and marker.read_text().strip() == content


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=5)
    args = ap.parse_args()
    gateway = json.loads((DEMO / 'gateway.json').read_text())
    original_policy = request(DASH, '/api/hook-control/status')['policy']
    cycles = []
    try:
        for i in range(args.rounds):
            old = iid_of(meta())
            restart_target()
            m = meta()
            new = iid_of(m)
            target = 'http://127.0.0.1:' + str(m['port'])
            pid = m['pid']
            cycle = {'old': old, 'new': new, 'identity_changed': old != new}
            # wait for the supervisor to auto-bind the new instance (no manual step)
            t0 = time.time()
            auto = False
            while time.time() - t0 < 45:
                if bound(new):
                    auto = True
                    break
                time.sleep(0.5)
            cycle['auto_bound'] = auto
            cycle['bind_wait_s'] = round(time.time() - t0, 1)
            # 3 IO rounds on the new instance + query latency
            session = request(target, '/session', {'title': 'restart io',
                'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}]})
            io_ok, lat = 0, []
            for _ in range(3):
                canary = 'ASG-RT-' + secrets.token_hex(6)
                started = chat_round(target, gateway, session['id'], canary)
                ok = wait_captured(new, canary, deadline=10.0)
                io_ok += 1 if ok else 0
                if ok:
                    lat.append(round(time.time() - started, 1))
            cycle['io_rounds_captured'] = io_ok
            cycle['query_latency_s'] = lat
            # 2 allow + 2 deny real effects on the new instance
            cycle['allow_ok'] = sum(1 for _ in range(2) if control_round(target, gateway, pid, 'allow'))
            cycle['deny_ok'] = sum(1 for _ in range(2) if control_round(target, gateway, pid, 'deny'))
            cycles.append(cycle)
    finally:
        request(DASH, '/api/hook-control/policy', original_policy)
    report = {'cycles': cycles, 'rounds': args.rounds, 'finished_at': time.time()}
    gates = {
        'five_restarts_ok': len(cycles) == args.rounds,
        'identity_correct_5_of_5': all(c['identity_changed'] for c in cycles),
        'auto_bound_5_of_5': all(c['auto_bound'] for c in cycles),
        'io_3_rounds_captured_5_of_5': all(c['io_rounds_captured'] >= 3 for c in cycles),
        'query_within_10s_5_of_5': all(c['query_latency_s'] and max(c['query_latency_s']) <= 10 for c in cycles),
        'allow_2_of_2_5_of_5': all(c['allow_ok'] == 2 for c in cycles),
        'deny_2_of_2_5_of_5': all(c['deny_ok'] == 2 for c in cycles),
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'restart-batch.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'report': str(path), 'gates': gates, 'passed': report['passed']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
