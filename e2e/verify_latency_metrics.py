"""Section-1 latency metrics: collection latency and page-update latency.

collection latency = (event produced) -> (locally queryable), measured against
each captured record's own producer timestamp while polling the local API.
page-update latency = (locally queryable) -> (page shows it), taken from the
dashboard's refresh interval (and re-checked live when a browser tab is used).

Usage: python3 e2e/verify_latency_metrics.py [--rounds 20]
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'artifacts/autonomous-demo'
DASH = 'http://127.0.0.1:8081'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(base, path, body=None, timeout=240, method='POST'):
    req = urllib.request.Request(base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'}, method=method)
    return json.load(OPENER.open(req, timeout=timeout))


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * len(ordered) + 0.5)) - 1))
    return ordered[index]


def page_refresh_seconds():
    source = (ROOT / 'web/dashboard.html').read_text(encoding='utf-8')
    intervals = [int(m.group(1)) for m in re.finditer(r'setInterval\(updateUI,\s*(\d+)\)', source)]
    return min(intervals) / 1000.0 if intervals else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=20)
    ap.add_argument('--instance', default=None)
    args = ap.parse_args()
    meta = json.loads((DEMO / 'target.json').read_text())
    gateway = json.loads((DEMO / 'gateway.json').read_text())
    target = 'http://127.0.0.1:' + str(meta['port'])
    instance = args.instance or ('%s:%s' % (meta['pid'], meta['create_time']))
    latencies = []
    for _ in range(args.rounds):
        canary = 'ASG-LAT-' + secrets.token_hex(8)
        sid = request(target, '/session',
                      {'title': 'latency', 'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}]},
                      timeout=60)['id']
        # Poll in parallel with the turn so we measure event->queryable, not turn duration.
        sent = []
        import threading

        def _send(sid=sid, canary=canary, sent=sent):  # bind loop vars; crazytest B023
            try:
                request(target, '/session/' + sid + '/message',
                        {'model': {'providerID': 'demo', 'modelID': gateway['model']},
                         'parts': [{'type': 'text', 'text': '请用中文简短确认收到 ' + canary}]}, timeout=240)
            except Exception:  # noqa: BLE001
                pass
            sent.append(time.time())

        threading.Thread(target=_send, daemon=True).start()
        started = time.time()
        visible_at = None
        while time.time() - started < 30:
            data = request(DASH, '/api/hook-data?' + urllib.parse.urlencode(
                {'instance_id': instance, 'limit': 200, 'max_bytes': 512 * 1024}), method='GET')
            for record in data.get('records') or []:
                if canary in json.dumps(record, ensure_ascii=False):
                    visible_at = time.time()
                    break
            if visible_at:
                break
            time.sleep(0.2)
        if not visible_at:
            latencies.append(None)
            continue
        # the record's own producer timestamp is the event time
        event_time = None
        for record in data.get('records') or []:
            if canary in json.dumps(record, ensure_ascii=False):
                event_time = record.get('timestamp')
                break
        latencies.append(round(visible_at - event_time, 3) if event_time else None)
    valid = [value for value in latencies if value is not None]
    report = {
        'instance': instance, 'rounds': args.rounds, 'samples': len(valid),
        'collection_latency_s': valid,
        'collection_p95_s': percentile(valid, 0.95),
        'collection_max_s': max(valid) if valid else None,
        'page_refresh_s': page_refresh_seconds(),
        'page_measurement_status': 'not_measured',
        'external_condition': '平台上报延迟属于任务 8，需 AI Trust 环境',
    }
    gates = {
        'samples_present': args.rounds >= 20 and len(valid) == args.rounds,
        'collection_p95_le_3s': report['collection_p95_s'] is not None and report['collection_p95_s'] <= 3.0,
        'collection_max_le_10s': report['collection_max_s'] is not None and report['collection_max_s'] <= 10.0,
        'page_update_le_5s': False  # Requires measured browser visibility, not timer configuration.,
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'latency-metrics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'gates': gates, 'passed': report['passed'],
                      'p95': report['collection_p95_s'], 'max': report['collection_max_s'],
                      'page_refresh_s': report['page_refresh_s'], 'samples': len(valid)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
