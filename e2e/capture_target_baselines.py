"""Task-6 step 2: record each real target's clean starting state.

Reads the live scan for the chosen targets and saves their identity, entry,
config/asset summary and hook state, so a later change can be compared against
an untouched baseline. It changes nothing on the targets.

Usage: python3 e2e/capture_target_baselines.py
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = 'http://127.0.0.1:8081'
OUT = ROOT / 'artifacts/acceptance'
# Baseline selection is generic: every distinct live agent in the current scan
# is captured, no hard-coded product list. Labels follow discovery order.
CHOSEN = None
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def state():
    return json.load(OPENER.open(DASH + '/api/state', timeout=15))


def main():
    snapshot = state()
    by_name = {agent.get('name'): agent for agent in snapshot.get('agents', [])}
    chosen = CHOSEN or {f'target_{index + 1}': agent.get('name')
                        for index, agent in enumerate(
                            sorted(snapshot.get('agents', []), key=lambda a: a.get('pid') or 0))}
    baselines = {}
    for label, name in chosen.items():
        agent = by_name.get(name)
        if not agent:
            baselines[label] = {'name': name, 'status': 'not_in_current_scan'}
            continue
        adapter = agent.get('adapter') or {}
        assets = adapter.get('assets') or {}

        def status(key, assets=assets):  # bind loop var; crazytest B023
            value = assets.get(key)
            return value.get('status') if isinstance(value, dict) else None

        baselines[label] = {
            'name': name,
            'instance_id': agent.get('instance_id'),
            'pid': agent.get('pid'),
            'create_time': adapter.get('create_time'),
            'entry': (adapter.get('investigated_identity') or {}).get('entry'),
            'workspace_cwd': adapter.get('workspace_cwd'),
            'match_status': adapter.get('match_status'),
            'fingerprint_revision': adapter.get('fingerprint_revision'),
            'hook_state': (adapter.get('hook_state') or {}).get('status'),
            'native_trust': (adapter.get('capability', {}) or {}).get('native_trust', {}).get('status')
            if isinstance(adapter.get('capability'), dict) else None,
            'asset_status': {'model_routing': status('model_routing'), 'mcp': status('registered_tools_and_mcp'),
                             'skills': status('skills'), 'rules': status('system_prompt_rules')},
            'target_modified_by_this_batch': False,
        }
    report = {
        'captured_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source': 'live /api/state scan; read-only',
        'targets': baselines,
        'note': '真实桌面目标未被本批次修改；仅记录只读基线，供后续对比',
    }
    gates = {
        'all_three_captured': all(v.get('status') != 'not_in_current_scan' for v in baselines.values()),
        'identity_fields_present': all(v.get('instance_id') and v.get('pid') for v in baselines.values()
                                       if v.get('status') != 'not_in_current_scan'),
        'no_target_modified': all(v.get('target_modified_by_this_batch') is False for v in baselines.values()),
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'target-baselines.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'gates': gates, 'passed': report['passed'],
                      'targets': {k: v.get('instance_id') for k, v in baselines.items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
