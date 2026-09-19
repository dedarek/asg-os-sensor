"""Independent real Codex app-server effect tests with an explicitly trusted learned Hook."""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e.verify_control_delivery import request


def main():
    folder = ROOT / 'artifacts/codex-acceptance'
    target = json.loads((folder / 'target.json').read_text())
    dashboard = 'http://127.0.0.1:8081'
    original = request(dashboard, '/api/hook-control/status')['policy']
    results = []
    try:
        for decision in ('deny', 'allow'):
            marker = folder / 'workspace' / ('effect-' + uuid.uuid4().hex + '.txt')
            content = uuid.uuid4().hex
            request(dashboard, '/api/hook-control/policy', {'default': original['default'],
                'rules': [{'tool': tool, 'pid': target['pid'], 'decision': decision}
                          for tool in ('Bash', 'apply_patch', 'Write', 'Edit')] + original['rules']})
            payload = {'prompt': f'In the current working directory, use exec_command exactly once to create the relative file {marker.name} containing exactly {content}. '
                       'If execution is denied, stop and report denial. Do not retry or use another tool.',
                       'output': decision + '-turn.json'}
            params = folder / 'effect-request.json'
            params.write_text(json.dumps(payload))
            started = time.time()
            subprocess.run([os.environ.get('ASG_TEST_NODE', '/Users/mac/.nvm/versions/node/v24.16.0/bin/node'),
                str(ROOT / 'e2e/codex_acceptance_turn.mjs'), str(folder), str(params)], check=True, timeout=330)
            events = request(dashboard, '/api/hook-control/status')['events']
            fresh = [e for e in events if e.get('pid') == target['pid'] and e.get('timestamp', 0) >= started
                     and e.get('event') == 'decision.returned' and e.get('decision') == decision
                     and marker.name in json.dumps(e.get('input'), ensure_ascii=False)]
            assert fresh, 'No decision for this actual requested file operation'
            if decision == 'deny':
                assert not marker.exists(), 'Denied operation created the file'
            else:
                assert marker.read_text().strip() == content, 'Allowed effect missing or wrong'
            results.append({'decision': decision, 'marker': str(marker), 'content': content,
                            'request_ids': [e['request_id'] for e in fresh], 'effect_verified': True})
    finally:
        request(dashboard, '/api/hook-control/policy', original)
    report = folder / 'control-verification.json'
    identity = {k: target[k] for k in ('pid', 'create_time')}
    report.write_text(json.dumps({'target': identity, 'results': results,
        'native_trust': target['hook_trust'], 'automatic_onboarding_verified': False,
        'source': 'Real Agent API tool execution; unique independent filesystem effect check'}, indent=2))
    os.environ['ASG_RUN_DIR'] = str(ROOT / 'artifacts/autonomous-service')
    from runtime.hook_acceptance import record
    record(identity, report, [Path('/Users/mac/.codex/asg-observer/hook.cjs'),
           Path('/Users/mac/.codex/asg-observer/run-hook.sh'), Path(target['codex_home']) / 'config.toml'],
           ['allow_effect', 'deny_effect'])
    print(json.dumps({'verified': True, 'report': str(report), 'pid': target['pid']}))


if __name__ == '__main__':
    main()
