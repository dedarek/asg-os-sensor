"""Task-6 measurement: candidate discovery and investigation visibility across
several real targets, plus the capability coverage each one has.

It measures only what can be observed safely against the live machine. Gates
that require launching/restarting/controlling a user's real desktop application
are recorded with an explicit external-condition reason instead of being
silently skipped or faked.

Usage: python3 e2e/verify_multi_target.py
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

DASH = 'http://127.0.0.1:8081'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path, timeout=15):
    return json.load(OPENER.open(DASH + path, timeout=timeout))


def post(path, body=None, timeout=60):
    req = urllib.request.Request(DASH + path,
        data=json.dumps(body).encode() if body is not None else b'',
        headers={'Content-Type': 'application/json'}, method='POST')
    return json.load(OPENER.open(req, timeout=timeout))


def agents_by_pid():
    return {a.get('pid'): a for a in get('/api/state').get('agents', [])}


def main():
    # Pick three real targets: an existing access base, WorkBuddy, and one that
    # has not been the focus target.
    live = agents_by_pid()
    by_name = {a.get('name'): pid for pid, a in live.items()}
    chosen = {}
    for label, name in (('access_base', 'OpenCode'), ('workbuddy', 'WorkBuddy AI'),
                        ('undisclosed', '@deepseek-ai/dsh')):
        if name in by_name:
            chosen[label] = {'name': name, 'pid': by_name[name]}
    report = {'chosen': chosen, 'started_at': time.time()}

    # 1) candidate discovery latency after a manual scan
    started = time.time()
    post('/api/scan')
    seen = None
    while time.time() - started < 30:
        current = agents_by_pid()
        if all(t['pid'] in current for t in chosen.values()):
            seen = time.time() - started
            break
        time.sleep(0.5)
    report['discovery_latency_s'] = round(seen, 1) if seen is not None else None

    # 2) capability coverage + investigation visibility per target
    per_target = []
    for label, t in chosen.items():
        state = agents_by_pid().get(t['pid']) or {}
        adapter = state.get('adapter') or {}
        cap = adapter.get('capability') or {}
        stages = {s['id']: s['state'] for s in cap.get('stages', [])}
        activity = {}
        try:
            activity = get('/api/investigation/activity?' + urllib.parse.urlencode(
                {'pid': t['pid'], 'limit': 5}))
        except Exception as exc:  # noqa: BLE001
            activity = {'error': type(exc).__name__}
        runs = activity.get('runs') or []
        first_activity = None
        if runs:
            first_activity = bool(runs[0].get('events')) or bool(runs[0].get('status'))
        per_target.append({
            'label': label, 'name': t['name'], 'pid': t['pid'],
            'match_status': adapter.get('match_status'),
            'proven': len(cap.get('proven', [])), 'stages': stages,
            'investigation_status': adapter.get('investigation', {}).get('status') if isinstance(adapter.get('investigation'), dict) else None,
            'activity_status': activity.get('status'), 'activity_runs': len(runs),
            'first_activity': first_activity,
        })
    report['targets'] = per_target

    # 3) gates that can be measured now
    gates = {
        'three_targets_selected': len(chosen) == 3,
        'candidate_discovery_within_30s': report['discovery_latency_s'] is not None,
        'investigation_visibility_exposed': all(
            x.get('activity_status') is not None or x.get('investigation_status') is not None
            for x in per_target),
        'coverage_recorded_per_target': all('stages' in x and x['proven'] > 0 for x in per_target),
    }
    report['gates'] = gates
    report['external_conditions'] = [
        '每个目标 10 轮输入输出与 ≥10 次工具记录：需要在隔离实例上驱动真实产品，当前仅 OpenCode 有隔离实例',
        '每个目标放行 5 / 拒绝 5：会对真实桌面应用产生副作用，需要隔离实例',
        '每个目标重启复用 3/3：需要能独立启动/重启该产品，不动用户正在使用的应用',
        '3/3 自主接入与 ≥1 个无原生 Hook 的替代接入：需要隔离实例与 Goose 完整调查（未完成调查不计通过）',
        '手工产品专用接入答案 0：由通用安装器路径保证，需在隔离实例上验证',
    ]
    report['finished_at'] = time.time()
    out = __import__('pathlib').Path(__file__).resolve().parents[1] / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'multi-target.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'chosen': chosen, 'discovery_latency_s': report['discovery_latency_s'],
                      'gates': gates}, ensure_ascii=False))
    for x in per_target:
        print(json.dumps(x, ensure_ascii=False))


if __name__ == '__main__':
    main()
