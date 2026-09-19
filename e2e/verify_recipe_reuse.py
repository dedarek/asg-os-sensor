"""Task-7 local pre-check: portability of a learned recipe and safe reuse.

No second machine here (that stays an external condition); this exercises what
can be done locally: export a portable bundle, prove it carries no source-machine
paths, credentials or chat text, prove the receiver resolves machine parameters
with zero manual edits, prove incompatible builds are never auto-installed, and
prove a failed installation rolls back.

Usage: python3 e2e/verify_recipe_reuse.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime import learned_install, recipe_bundle  # noqa: E402

FINGERPRINT = 'harness-efac4c5f21c6'
DB_PATH = ROOT / 'artifacts/autonomous-service/fingerprints.json'


def forced_failure_round(workspace: Path, state_dir: Path) -> dict:
    """Install a 2-file plan whose second write fails; expect a clean rollback."""
    (workspace / 'blocker').write_text('this is a file, not a directory\n')
    plan = {'version': 1, 'files': [
        {'path': 'a.txt', 'content': 'first', 'expected_sha256': None},
        {'path': 'blocker/child.txt', 'content': 'second', 'expected_sha256': None},
    ]}
    digest = learned_install.plan_digest(learned_install.validate_plan(plan))
    raised = False
    try:
        learned_install.install(plan, workspace, state_dir,
                                approved_workspace=workspace, approved_digest=digest)
    except Exception as exc:  # noqa: BLE001
        raised = True
        error = type(exc).__name__
    return {'raised': raised, 'error': locals().get('error'),
            'first_file_rolled_back': not (workspace / 'a.txt').exists(),
            'blocker_intact': (workspace / 'blocker').is_file()}


def main():
    report = {'fingerprint': FINGERPRINT}
    db = json.loads(DB_PATH.read_text())
    entry = next(f for f in db['fingerprints'] if f.get('id') == FINGERPRINT)
    source_ws = (entry['hook_recipe'].get('hook') or {}).get('workspace')
    bundle = recipe_bundle.export_bundle(FINGERPRINT, db=db, asg_root=str(ROOT),
                                         target_workspace=source_ws, note='task7 local precheck')
    scan = recipe_bundle.scan_portability(bundle, asg_root=str(ROOT), target_workspace=source_ws)
    report['bundle_bytes'] = len(json.dumps(bundle, ensure_ascii=False))
    report['machine_parameters'] = bundle.get('machine_parameters')
    report['scan'] = scan
    report['transit_integrity_ok'] = recipe_bundle.validate_bundle(bundle)['ok']

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()  # macOS /var is a symlink; the installer rejects symlink paths
        receiver_root = base / 'machineB' / 'asg'
        receiver_ws = base / 'machineB' / 'workspace'
        receiver_ws.mkdir(parents=True)
        resolved = recipe_bundle.resolve_bundle(bundle, asg_root=receiver_root, target_workspace=receiver_ws)
        resolved_text = json.dumps(resolved, ensure_ascii=False)
        report['receiver_resolution'] = {
            'source_root_absent': str(ROOT) not in resolved_text,
            'receiver_root_present': str(receiver_root) in resolved_text,
            'receiver_workspace_present': str(receiver_ws) in resolved_text,
        }

        stored = (bundle.get('constraints') or {}).get('compatibility') or {}
        # exact build -> plan ready, but import writes nothing
        exact = recipe_bundle.import_bundle(resolved, observed_build=dict(stored), workspace=str(receiver_ws))
        report['exact_import'] = {'status': exact['status'],
                                  'files_written': len(list(receiver_ws.rglob('*')))}

        # three incompatible variants -> never auto-installed
        variants = []
        for key in ('executable', 'entry', 'platform'):
            observed = dict(stored)
            observed[key] = 'CHANGED-' + str(key)
            result = recipe_bundle.import_bundle(resolved, observed_build=observed, workspace=str(receiver_ws))
            variants.append({'changed': key, 'status': result['status'],
                             'compat': result['compatibility']['status']})
        report['incompatible_trials'] = variants
        report['incompatible_wrote_files'] = len(list(receiver_ws.rglob('*')))

    rollbacks = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        for i in range(3):
            ws = base / ('ws-%d' % i)
            state = base / ('state-%d' % i)
            ws.mkdir(parents=True)
            state.mkdir(parents=True)
            rollbacks.append(forced_failure_round(ws, state))
    report['rollbacks'] = rollbacks

    gates = {
        'no_machine_paths_in_bundle': not scan['machine_paths'],
        'no_credentials_in_bundle': not scan['credential_hits'],
        'no_chat_content_in_bundle': not scan['chat_hits'],
        'receiver_resolves_without_manual_edits': report['receiver_resolution']['source_root_absent']
        and report['receiver_resolution']['receiver_root_present']
        and report['receiver_resolution']['receiver_workspace_present'],
        'exact_import_ready_but_not_installed': report['exact_import']['status'] == 'ready_for_authorization'
        and report['exact_import']['files_written'] == 0,
        'three_incompatible_rejected_no_wrong_install': all(v['compat'] != 'exact' for v in variants)
        and report['incompatible_wrote_files'] == 0,
        'three_failed_installs_rolled_back': all(r['raised'] and r['first_file_rolled_back'] for r in rollbacks),
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    report['external_condition'] = '跨机器验收仍需第二台真实机器在 B 上完成安装、加载与独立验收'
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'recipe-reuse.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'gates': gates, 'passed': report['passed']}, ensure_ascii=False))
    print('scan', json.dumps(scan, ensure_ascii=False))
    print('receiver', json.dumps(report['receiver_resolution'], ensure_ascii=False))
    print('rollbacks', json.dumps(rollbacks, ensure_ascii=False))


if __name__ == '__main__':
    main()
