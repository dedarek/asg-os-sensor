"""Single repeatable acceptance entry point, in the task-book's fixed order.

Runs the acceptance programs in order (evidence -> single-target capture ->
control -> restart -> independent -> generalization -> cross-machine ->
AI Trust), records pass/fail and duration for each, then regenerates the
sign-off table. --quick runs the fast deterministic programs; the default runs
the full locally-runnable set.

Usage: python3 e2e/run_all_acceptance.py [--quick] [--list]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/acceptance'

# (order, name, argv, needs_live_agent, quick, note)
PROGRAMS = [
    (1, 'batch_and_rules', ['e2e/verify_acceptance_rules.py'], False, True,
     '统一验收批次与第一节规则'),
    (2, 'io_batch', ['e2e/verify_io_batch.py', '--scenarios', 'all'], True, False,
     '单目标采集对账（30 轮等固定批次）'),
    (3, 'io_extra', ['e2e/verify_io_extra.py', '--rounds', '5', '--attach', '3'], True, False,
     '取消/子 Agent/附件'),
    (4, 'control_batch', ['e2e/verify_control_batch.py'], True, False,
     '放行 20 / 拒绝 20 / 并发 / 性能'),
    (5, 'control_confirm', ['e2e/verify_control_confirm_batch.py', '--rounds', '5'], True, False,
     '人工确认/超时/中断/重复/参数替换'),
    (6, 'control_actions', ['e2e/verify_control_actions.py', '--rounds', '3'], True, False,
     '三类可核对动作'),
    (7, 'restart_batch', ['e2e/verify_restart_batch.py', '--rounds', '5'], True, False,
     '重启后持续接管'),
    (8, 'capture_bound', ['e2e/verify_capture_bound.py'], True, False,
     '采集上限保留完整载荷'),
    (9, 'independent_runtime', ['e2e/verify_independent_runtime.py', '--soak-min', '0'], False, False,
     '独立运行（发现与 Goose 停止）'),
    (10, 'portable_install', ['e2e/verify_portable_install.py'], False, True,
     '闭包式安装可独立启动'),
    (11, 'multi_target', ['e2e/verify_multi_target.py'], False, True,
     '多目标候选发现与调查可见性'),
    (12, 'access_wrapper', ['e2e/verify_access_wrapper.py'], False, True,
     '替代接入（受控模型代理）'),
    (13, 'recipe_reuse', ['e2e/verify_recipe_reuse.py'], False, True,
     '配方可移植与回滚'),
    (14, 'ai_trust_connector', ['e2e/verify_ai_trust_connector.py'], False, True,
     'AI Trust 连接器（本地验证）'),
    (15, 'missing_events', ['e2e/verify_missing_events.py'], False, True,
     '缺失事件来源调查'),
    (16, 'latency_metrics', ['e2e/verify_latency_metrics.py', '--rounds', '20'], True, False,
     '采集/页面更新延迟'),
    (17, 'target_baselines', ['e2e/capture_target_baselines.py'], False, True,
     '三目标只读基线'),
]
REPORT = ['e2e/acceptance_report.py']


def run(relative_argv):
    started = time.time()
    try:
        proc = subprocess.run([sys.executable, *relative_argv], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return {'returncode': None, 'seconds': round(time.time() - started, 1),
                'declared_passed': None, 'ok': False, 'tail': ['验收程序超时，未通过']}
    stdout = proc.stdout or ''
    declared = None
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith('{') and '"passed"' in line:
            try:
                declared = bool(json.loads(line).get('passed'))
            except ValueError:
                declared = None
    ok = proc.returncode == 0 and declared is True
    return {'returncode': proc.returncode, 'seconds': round(time.time() - started, 1),
            'declared_passed': declared, 'ok': ok,
            'tail': (stdout or proc.stderr or '').strip().splitlines()[-1:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--only', default='')
    args = ap.parse_args()

    selected = [p for p in PROGRAMS if (p[4] if args.quick else True)]
    if args.only:
        wanted = set(args.only.split(','))
        selected = [p for p in PROGRAMS if p[1] in wanted]
    if args.list:
        for order, name, argv, live, quick, note in PROGRAMS:
            print('%2d %-20s quick=%-5s live=%-5s %s' % (order, name, quick, live, note))
        return 0

    results = []
    for order, name, argv, live, quick, note in selected:
        outcome = run(argv)
        outcome.update({'order': order, 'name': name, 'note': note})
        results.append(outcome)
        print('%2d %-20s %s (%ss)' % (order, name, 'OK' if outcome['ok'] else 'FAIL', outcome['seconds']),
              flush=True)
    table = run(REPORT)
    report = {
        'mode': 'quick' if args.quick else 'full',
        'programs': results,
        'passed': all(r['ok'] for r in results),
        'acceptance_report': table,
        'external_conditions': [
            'AI Trust 测试环境 + 接入协议 + 测试凭证',
            '第二台机器（跨机器复用）',
            '专用测试机器（机器重启）',
            'WorkBuddy 与一个无原生 Hook 目标的隔离实例',
        ],
        'finished_at': time.time(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'run-all-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'mode': report['mode'], 'programs': len(results),
                      'passed': report['passed'],
                      'failed': [r['name'] for r in results if not r['ok']]}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
