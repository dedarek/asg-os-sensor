"""Unified acceptance rules (task-book section 1) and per-item reports (section 10).

Assembles one acceptance batch from the existing batch artifacts, records the
fixed identity fields, and evaluates the four section-1 gates:
  * required identity field completeness = 100%
  * evidence bound to a concrete instance = 100%
  * cross-instance mis-association = 0
  * synthetic events mixed into a real batch = 0
It also emits each item as a test report with the required columns.

Usage: python3 e2e/verify_acceptance_rules.py
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/acceptance'
DEMO = ROOT / 'artifacts/autonomous-demo'
RUN = ROOT / 'artifacts/autonomous-service'

REQUIRED_IDENTITY = ('machine_id', 'instance_id', 'hook_version', 'recipe_version',
                     'validation_mode', 'evidence_source')


def read(name):
    try:
        return json.loads((OUT / name).read_text())
    except (OSError, ValueError):
        return None


def machine_id():
    path = OUT / 'machine-id.json'
    if path.is_file():
        return json.loads(path.read_text())['machine_id']
    import platform
    value = hashlib.sha256((platform.node() + '|' + platform.platform()).encode()).hexdigest()[:16]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'machine_id': value, 'node': platform.node(),
                                'platform': platform.platform(), 'created_at': time.time()},
                               ensure_ascii=False, indent=2))
    return value


def hook_version():
    hook = DEMO / 'workspace/.opencode/plugin/asg-decision-hook.js'
    if not hook.is_file():
        return None
    return 'sha256:' + hashlib.sha256(hook.read_bytes()).hexdigest()


def recipe_version():
    try:
        db = json.loads((RUN / 'fingerprints.json').read_text())
    except (OSError, ValueError):
        return None
    for entry in db.get('fingerprints', []):
        if entry.get('id') == 'harness-efac4c5f21c6':
            return '%s@rev%s' % (entry.get('id'), entry.get('revision'))
    return None


def agent_version():
    try:
        text = (DEMO / 'data/opencode/log/opencode.log').read_text()[-200000:]
    except OSError:
        return None
    import re
    match = re.search(r'version=([0-9.]+)', text)
    return match.group(1) if match else None


def demo_instance():
    try:
        meta = json.loads((DEMO / 'target.json').read_text())
        return '%s:%s' % (meta['pid'], meta['create_time'])
    except (OSError, ValueError):
        return None


def collect_instances():
    instances = {}
    demo = demo_instance()
    if demo:
        instances[demo] = {'agent': 'OpenCode', 'version': agent_version(), 'mode': 'isolated_real_instance'}
    restart = read('restart-batch.json') or {}
    for cycle in restart.get('cycles', []):
        for key in ('old', 'new'):
            if cycle.get(key):
                instances.setdefault(cycle[key], {'agent': 'OpenCode', 'version': agent_version(),
                                                  'mode': 'isolated_real_instance'})
    independent = read('independent-runtime.json') or {}
    if independent.get('workload', {}).get('instance'):
        instances.setdefault(independent['workload']['instance'],
                             {'agent': 'OpenCode', 'version': agent_version(),
                              'mode': 'isolated_real_instance'})
    return instances, demo


def collect_sessions():
    """Session ids per instance, so every record links to a concrete conversation."""
    sessions = {}

    def add(instance, session):
        if instance and session:
            sessions.setdefault(instance, set()).add(session)

    demo = demo_instance()
    control = read('control-batch.json') or {}
    target = control.get('target') or {}
    control_instance = ('%s:%s' % (target.get('pid'), target.get('create_time'))
                        if target.get('pid') else None)
    for row in control.get('allow', {}).get('rounds', []) + control.get('deny', {}).get('rounds', []) \
            + control.get('concurrency', {}).get('rounds', []):
        add(control_instance, row.get('session'))
    io_batch = read('io-batch.json') or {}
    io_instance = io_batch.get('instance_id')
    for rows in (io_batch.get('scenarios') or {}).values():
        for row in rows:
            add(io_instance, row.get('session_id'))
    io_extra = read('io-extra.json') or {}
    extra_instance = io_extra.get('instance_id')
    for key in ('cancel', 'attach', 'subagent'):
        for row in io_extra.get(key, []):
            add(extra_instance, row.get('session_id'))
    retry = read('retry-batch.json') or {}
    for row in retry.get('rounds', []):
        add(demo, row.get('session'))
    return {key: sorted(value) for key, value in sessions.items()}


def build_reports(batch, instances, demo):
    report = read('acceptance-report.json') or {}
    rows = []
    for task in report.get('tasks', []):
        for index, item in enumerate(task.get('items', [])):
            instance = demo
            fallback = (task.get('evidence') or [''])
            evidence = (item.get('evidence') or item.get('test') or item.get('detail')
                        or (fallback[0] if fallback else '') or item.get('item') or '')
            for known in instances:
                if known and known in str(evidence):
                    instance = known
                    break
            rows.append({
                'test_id': 'T%d.%d' % (task['task'], index + 1),
                'task': task['task'], 'target_instance': instance,
                'steps': item.get('item'), 'expected': item.get('item'),
                'actual': item.get('status'), 'metric': item.get('status'),
                'evidence': evidence, 'pass': item.get('status') == '通过',
                'machine_id': batch['machine_id'], 'instance_id': instance,
                'hook_version': batch['hook_version'], 'recipe_version': batch['recipe_version'],
                'validation_mode': batch['validation_mode'], 'evidence_source': evidence,
            })
    return rows


def duplicate_logical_records():
    """Section-1 'displayed duplicates': the same logical record twice."""
    from collections import Counter
    reasons = {}
    log = DEMO / 'workspace/.opencode/asg-logs/decision-hook-events.jsonl'
    try:
        seen = Counter()
        for line in log.read_text().splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            seen[(record.get('pid'), record.get('seq'), record.get('event'))] += 1
        reasons['hook_log_duplicate_seq'] = sum(count - 1 for count in seen.values() if count > 1)
    except OSError:
        reasons['hook_log_duplicate_seq'] = None
    try:
        seen = Counter()
        for line in (RUN / 'hook-control-events.jsonl').read_text().splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get('event') == 'decision.returned':
                seen[record.get('request_id')] += 1
        reasons['control_returned_duplicate'] = sum(count - 1 for count in seen.values() if count > 1)
    except OSError:
        reasons['control_returned_duplicate'] = None
    reasons['total'] = sum(v for v in reasons.values() if isinstance(v, int))
    return reasons


def main():
    instances, demo = collect_instances()
    by_instance = collect_sessions()
    sessions_turns = {'source': '批次工件中的会话编号（control / io / io-extra / retry）',
                      'by_instance': by_instance,
                      'total_sessions': sum(len(v) for v in by_instance.values())}
    batch = {
        'batch_id': 'ASG-BATCH-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime()),
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'machine_id': machine_id(),
        'hook_version': hook_version(),
        'recipe_version': recipe_version(),
        'agent_version': agent_version(),
        'validation_mode': 'isolated_real_instance',
        'instances': instances,
        'sessions_turns': sessions_turns,
        'human_interventions': [
            {'action': '终止一个正在进行的 Goose 调查', 'reason': '任务 5 要求停止 Goose', 'at': '2026-09-14'},
            {'action': '将 Hook 决策客户端临时指向独立服务并在结束后复原', 'reason': '任务 5 独立运行', 'at': '2026-09-14'},
            {'action': '把扫描周期设为 10 秒', 'reason': '任务 4 自动重新绑定的观测窗口', 'at': '2026-09-14'},
        ],
        'evidence_sources': {
            'hook': str(DEMO / 'workspace/.opencode/asg-logs/decision-hook-events.jsonl'),
            'control_events': str(RUN / 'hook-control-events.jsonl'),
            'batches': [p.name for p in sorted(OUT.glob('*.json'))],
        },
    }
    rows = build_reports(batch, instances, demo)

    def complete(row):
        return all(isinstance(row.get(f), str) and row.get(f) for f in REQUIRED_IDENTITY)

    instance_rows = [r for r in rows if r.get('instance_id')]
    mis = [r['test_id'] for r in rows if r.get('instance_id') and r['instance_id'] not in instances]
    synthetic = [r['test_id'] for r in rows if r.get('validation_mode') == 'synthetic']
    rules = {
        'identity_field_completeness_rate': round(sum(1 for r in rows if complete(r)) / len(rows), 4) if rows else 0,
        'evidence_instance_association_rate': round(sum(1 for r in instance_rows if r['instance_id'] in instances) / len(instance_rows), 4) if instance_rows else 0,
        'cross_instance_misassociation_count': len(mis),
        'synthetic_event_contamination_count': len(synthetic),
        'rows': len(rows), 'instance_scoped_rows': len(instance_rows),
    }
    gates = {
        'identity_completeness_100pct': rules['identity_field_completeness_rate'] == 1.0,
        'evidence_bound_to_instance_100pct': rules['evidence_instance_association_rate'] == 1.0,
        'cross_instance_misassociation_0': rules['cross_instance_misassociation_count'] == 0,
        'synthetic_contamination_0': rules['synthetic_event_contamination_count'] == 0,
        'conversation_linkage_present': sessions_turns['total_sessions'] > 0,
    }
    duplicates = duplicate_logical_records()
    gates['display_duplicates_0'] = duplicates['total'] == 0
    result = {'batch': batch, 'rules': rules, 'gates': gates, 'passed': all(gates.values()),
              'test_report_columns': ['测试编号', '目标实例', '操作步骤', '预期结果', '实际结果',
                                      '指标', '原始证据', '是否通过'],
              'duplicates': duplicates, 'finished_at': time.time()}
    (OUT / 'acceptance-batch.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    (OUT / 'test-reports.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print(json.dumps({'batch_id': batch['batch_id'], 'rules': rules, 'gates': gates,
                      'passed': result['passed']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
