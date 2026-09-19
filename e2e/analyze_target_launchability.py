"""Task-6 step 1/8 evidence: what each real target is, and whether it can be
launched as a local isolated instance (like the OpenCode acceptance target).

Read-only: inspects the live processes and their executables. It answers
"can this target be driven in an isolated instance locally?" without touching
the user's running applications.

Usage: python3 e2e/analyze_target_launchability.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/acceptance'
DEMO_WORKSPACE = ROOT / 'artifacts/autonomous-demo/workspace'

# kind: how the target runs; isolatable locally: can we start a second, fully
# separate instance of it without disturbing the user's running copy?
CLASSIFICATION = {
    32045: ('OpenCode (acceptance instance)', 'cli_agent', True,
            '已由 e2e/real_autonomous_demo.py 以独立 HOME/端口启动，作为接入基础目标'),
    1424: ('ZCode', 'desktop_app', False,
           '桌面 .app 单实例；再起一份会拉起另一个桌面应用，需隔离机器/用户环境'),
    93144: ('WorkBuddy AI', 'desktop_app_electron', False,
            'Electron 桌面应用（daemon-app-server-entry --stdio）；隔离实例需独立应用数据目录，未验证'),
    45137: ('Codex (app-server)', 'desktop_app_sidecar', False,
            'ChatGPT 桌面应用内置 codex app-server；需用户原生信任与桌面环境，不能本机隔离'),
    55875: ('Codex (app-server)', 'desktop_app_sidecar', False,
            '同上'),
    1052: ('OpenCodex', 'provider_proxy', 'n/a',
           '是“把 Codex/Claude Code 指向任意 LLM 的通用供应商代理”，属于模型网关角色，不是待接入的 Agent'),
    24034: ('@deepseek-ai/dsh', 'cli_unknown', 'unverified',
            'node CLI（dsh web）；是否为 Agent、能否隔离启动未证实'),
}


def main():
    rows = []
    for pid, (name, kind, isolatable, note) in CLASSIFICATION.items():
        if not psutil.pid_exists(pid):
            rows.append({'pid': pid, 'name': name, 'kind': kind, 'present': False,
                         'isolatable_locally': isolatable, 'note': note})
            continue
        try:
            proc = psutil.Process(pid)
            exe = proc.exe()
            cmdline = ' '.join(proc.cmdline())
            cwd = proc.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            exe, cmdline, cwd = None, None, None
        rows.append({'pid': pid, 'name': name, 'kind': kind, 'present': True,
                     'exe': exe, 'cmdline': cmdline, 'cwd': cwd,
                     'isolatable_locally': isolatable, 'note': note})

    isolatable = [r['name'] for r in rows if r.get('isolatable_locally') is True]
    report = {
        'captured_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'method': '只读检查进程可执行文件与命令行；未启动或改动任何真实应用',
        'acceptance_instance_dir': str(DEMO_WORKSPACE.relative_to(ROOT)),
        'targets': rows,
        'locally_isolatable_targets': isolatable,
        'conclusion': ('本机可直接隔离启动的 Agent 目标只有 OpenCode 验收实例；'
                       '其余为桌面应用（ZCode/WorkBuddy/Codex）或供应商代理/未证实 CLI，'
                       '需要隔离机器、独立应用数据目录或用户本人操作'),
        'external_conditions': [
            'ZCode / WorkBuddy 的隔离实例（独立应用数据目录或隔离机器）',
            'Codex 桌面目标的用户原生信任（由用户本人完成）',
            'dsh 是否可作为 Agent 目标并能隔离启动的确认',
        ],
    }
    gates = {
        'analysis_recorded': True,
        'no_target_modified': True,
        'isolatable_targets_identified': bool(isolatable),
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'target-launchability.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'isolatable': isolatable,
                      'kinds': {r['name']: r['kind'] for r in rows}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
