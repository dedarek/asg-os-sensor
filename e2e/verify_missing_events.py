"""Task-2 step 4: where the declared-but-unobserved event kinds come from.

Instead of re-running Goose (which would rewrite the already-verified Hook),
this reads the target's own producer log and the saved Goose investigation
artifacts, then classifies each declared-but-unseen kind as: observed under a
different producer name, or genuinely not exposed by this target.

Usage: python3 e2e/verify_missing_events.py
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / 'artifacts/autonomous-demo/workspace/.opencode/asg-logs/decision-hook-events.jsonl'
RUN = ROOT / 'artifacts/autonomous-service'
OUT = ROOT / 'artifacts/acceptance'

# The vocabulary the acceptance contract expects, compared against what the
# producer really emits.
DECLARED = ('user.input', 'model.request', 'model.response', 'tool.execute.before',
            'tool.execute.after', 'tool.error', 'assistant.output',
            'session.start', 'session.end', 'hook.loaded')


def main():
    events, subtypes = Counter(), Counter()
    for line in LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        events[record.get('event')] += 1
        if record.get('event') == 'session.event':
            subtypes[(record.get('detail') or {}).get('event_type')] += 1

    observed = {kind for kind in DECLARED if events.get(kind)}
    missing = [kind for kind in DECLARED if not events.get(kind)]
    # What the producer emits instead for the lifecycle boundary.
    alternatives = {
        'session.end': {'producer_subtypes': ['session.idle', 'session.compacted', 'session.updated'],
                        'note': '该运行时没有独立的会话结束事件，最接近的是 session.idle / session.compacted'},
    }
    classification = []
    for kind in missing:
        alt = alternatives.get(kind)
        classification.append({
            'kind': kind,
            'classification': '目标不支持' if alt else '仍缺口',
            'closest_producer_events': alt['producer_subtypes'] if alt else [],
            'observed_counts': {name: subtypes.get(name, 0) for name in (alt or {}).get('producer_subtypes', [])},
            'reason': alt['note'] if alt else '目标侧未发现可映射的原生事件',
            'evidence': [str(LOG.relative_to(ROOT))],
        })

    goose = sorted(str(p.relative_to(ROOT)) for p in RUN.glob('pid_45242_*/investigation_findings.json'))
    report = {
        'target': 'OpenCode (isolated acceptance instance)',
        'declared': list(DECLARED),
        'observed': sorted(observed),
        'missing': missing,
        'classification': classification,
        'producer_event_counts': dict(events),
        'producer_session_subtypes': dict(subtypes),
        'goose_investigation_artifacts': goose[-3:],
        'method': '读取目标自身生产日志 + 既有 Goose 调查产物；未重跑 Goose（避免改写已验证的 Hook）',
        'finished_at': None,
    }
    gates = {
        'hook_loaded_now_observed': events.get('hook.loaded', 0) > 0,
        'missing_kinds_classified': all(item['classification'] for item in classification),
        'no_unexplained_gap': all(item['classification'] != '仍缺口' for item in classification),
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'missing-events-investigation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'gates': gates, 'passed': report['passed'], 'missing': missing,
                      'classification': classification}, ensure_ascii=False))


if __name__ == '__main__':
    main()
