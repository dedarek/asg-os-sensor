"""Delivery note (task-book task 5): the resident components, item by item.

Reads the independent-runtime manifest and the acceptance batch, writes a human
delivery note and a machine-readable copy. Only discovery may be removed.

Usage: python3 e2e/generate_delivery_note.py
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/acceptance'
RUN = ROOT / 'artifacts/autonomous-service'

RESIDENT = [
    ('hook_runtime.py', '独立运行服务的启停脚本（start/stop/status），含进程身份校验'),
    ('runtime/hook_service.py', '独立运行服务本体：事件、对话、控制决策；不扫描、不调查、不学配方'),
    ('runtime/hook_control.py', '同步决策引擎（放行/拒绝/人工确认、fail-closed、幂等）'),
    ('runtime/hook_control_client.py', '目标侧决策客户端（Hook 通过它同步获取决策）'),
    ('runtime/hook_data.py', '有界日志读取与归一化（含 conversation 与词表覆盖）'),
    ('runtime/observation_registry.py', '观测绑定登记（实例级历史绑定）'),
    ('runtime/observation_source.py', '观测源校验与快照'),
    ('runtime/event_vocabulary.py', '原生事件名到统一词表的翻译'),
    ('runtime/learned_install.py', '文件计划安装与回滚（备份/原子写）'),
    ('runtime/file_lock.py', '跨进程单写者文件锁（独立于匹配器，供安装与指纹库共用）'),
    ('runtime/analyst_evidence.py', '载荷脱敏'),
    ('runtime/hook_acceptance.py / runtime/io_acceptance.py', '输入输出验收契约（计数/配对/检查点）'),
]
REMOVABLE = [
    ('monitor_dashboard.py 的进程扫描与展示部分', '仅在发现/展示阶段需要，可退出'),
    ('Goose 调查与配方学习（recipes/、analyst_*）', '学习阶段需要，运行阶段不需要'),
    ('find_agents*.py、matcher.py（指纹匹配与扫描入口）', '发现/匹配入口；运行闭包已不再依赖它（锁已独立到 file_lock.py）'),
    ('runtime/llm_proxy.py 的捕获配置', '仅在显式测试网关下需要'),
]


def read(name):
    try:
        return json.loads((OUT / name).read_text())
    except (OSError, ValueError):
        return None


def main():
    independent = read('independent-runtime.json') or {}
    portable = read('portable-install.json') or {}
    manifest = independent.get('manifest') or {}
    batch = (read('acceptance-batch.json') or {}).get('batch') or {}
    report = read('acceptance-report.json') or {}
    statuses = {t['task']: t['status'] for t in report.get('tasks', [])}

    delivery = {
        'generated_at': date.today().isoformat(),
        'batch_id': batch.get('batch_id'),
        'machine_id': batch.get('machine_id'),
        'resident_components': [{'path': p, 'role': r} for p, r in RESIDENT],
        'removable_components': [{'component': c, 'reason': r} for c, r in REMOVABLE],
        'runtime_dependency_files': manifest.get('files'),
        'runtime_interpreter': manifest.get('interpreter'),
        'discovery_dependencies': manifest.get('discovery_dependencies'),
        'service_files': {
            'settings': str(RUN / 'hook-runtime.json'),
            'runtime_state': str(RUN / 'hook-runtime-state.json'),
            'control_client_config': str(RUN / 'hook-control-client.json'),
            'control_token': str(RUN / 'hook-control.token'),
            'bindings': str(RUN / 'observations.json'),
            'bound_log_example': 'artifacts/autonomous-demo/workspace/.opencode/asg-logs/decision-hook-events.jsonl',
        },
        'endpoints': ['/health', '/api/instances', '/api/hook-data', '/api/conversation',
                      '/api/hook-control/status', '/api/hook-control/decision'],
        'commands': {
            'start': 'python3 hook_runtime.py start --config artifacts/autonomous-service/hook-runtime.json',
            'status': 'python3 hook_runtime.py status --config artifacts/autonomous-service/hook-runtime.json',
            'stop': 'python3 hook_runtime.py stop --config artifacts/autonomous-service/hook-runtime.json',
        },
        'verified_while_discovery_stopped': {
            'soak_minutes': (independent.get('soak') or {}).get('minutes'),
            'soak_samples': (independent.get('soak') or {}).get('samples'),
            'chat_rounds': (independent.get('workload') or {}).get('chat_rounds'),
            'tool_calls': (independent.get('workload') or {}).get('tool_calls'),
            'outage_events': (independent.get('outage') or {}).get('events_in_window'),
            'catch_up_seconds': (independent.get('outage') or {}).get('catch_up_seconds'),
        },
        'portable_install': {
            'closure_files': manifest.get('files'),
            'discovery_files_present_in_install': portable.get('discovery_files_present_in_install'),
            'instances_served_from_isolated_run_dir': portable.get('instances_served'),
            'passed': portable.get('passed'),
        },
        'task_status': statuses,
        'open_local_acceptance': [
            '当前正常桌面 Codex 再完成 9 个完整回合，使独立逐字对账达到 10 回合（任务 2）',
            '独立 Hook 服务连续运行并采样满 60 分钟（任务 5）',
            'WorkBuddy 与一个无原生 Hook 目标完成各自采集、控制和重启固定批次（任务 6）',
            '在浏览器内测量事件产生到页面可见的实际延迟（统一延迟指标）',
        ],
        'open_external_conditions': [
            'AI Trust 测试环境 + 接入协议 + 测试凭证（任务 8 平台侧）',
            '第二台机器（任务 7 跨机器验收）',
            '专用测试机器（任务 5 机器重启后 60 秒恢复）',
        ],
    }
    (OUT / 'delivery.json').write_text(json.dumps(delivery, ensure_ascii=False, indent=2))

    lines = ['# 交付说明（%s）' % delivery['generated_at'], '',
             '验收批次：%s；机器：%s。' % (delivery['batch_id'], delivery['machine_id']),
             '', '## 一、必须保留的常驻组件（逐项）', '']
    for index, (path, role) in enumerate(RESIDENT, 1):
        lines.append('%d. `%s` — %s' % (index, path, role))
    lines += ['', '运行需要的文件与配置：', '']
    for key, value in delivery['service_files'].items():
        lines.append('- %s：`%s`' % (key, value))
    lines += ['', '运行依赖（闭包检查）：%s' % ('、'.join(manifest.get('files') or [])),
              '', '解释器：%s' % manifest.get('interpreter'),
              '', '对发现模块的运行时依赖：%s' % (manifest.get('discovery_dependencies') or '无'),
              '', '## 二、可以删除的发现部分', '']
    for component, reason in REMOVABLE:
        lines.append('- %s —— %s' % (component, reason))
    lines += ['', '## 三、启停与状态', '', '```bash']
    for name, command in delivery['commands'].items():
        lines.append('# %s\n%s' % (name, command))
    lines += ['```', '', '服务端点：%s' % '、'.join(delivery['endpoints']),
              '', '## 四、最近独立运行批次记录（以验收总表判定为准）', '',
              '- 独立运行 %s 分钟，采样 %s 次（0 次表示本轮未执行耐久测试）' % (delivery['verified_while_discovery_stopped']['soak_minutes'],
                                                       delivery['verified_while_discovery_stopped']['soak_samples']),
              '- 期间完成 %s 轮对话、%s 次工具调用' % (delivery['verified_while_discovery_stopped']['chat_rounds'],
                                                  delivery['verified_while_discovery_stopped']['tool_calls']),
              '- 停服期间产生 %s 条事件，恢复后 %s 秒补齐' % (delivery['verified_while_discovery_stopped']['outage_events'],
                                                       delivery['verified_while_discovery_stopped']['catch_up_seconds']),
              '- 闭包式安装（仅复制运行依赖到新目录）：启动正常并服务 %s 个已绑定实例；安装目录中无发现模块文件（%s）'
              % (delivery['portable_install']['instances_served_from_isolated_run_dir'],
                 delivery['portable_install']['discovery_files_present_in_install']),
              '', '## 五、任务签收状态', '']
    for task, status in sorted(statuses.items()):
        lines.append('- 任务 %s：%s' % (task, status))
    lines += ['', '## 六、本机仍未签收', '']
    for item in delivery['open_local_acceptance']:
        lines.append('- %s' % item)
    lines += ['', '## 七、用户已明确本轮不做的外部验收', '']
    for item in delivery['open_external_conditions']:
        lines.append('- %s' % item)
    path = ROOT / 'docs/stage1' / ('DELIVERY_NOTE_%s.md' % date.today().strftime('%Y%m%d'))
    path.write_text('\n'.join(lines) + '\n')
    print(json.dumps({'note': str(path), 'json': str(OUT / 'delivery.json'),
                      'resident': len(RESIDENT)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
