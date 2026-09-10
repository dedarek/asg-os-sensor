"""Bridge an evidence-bound Goose candidate to the generic file installer.

No product registry or application template is consulted. This is an explicit
PoC execution path: the supervisor must approve the exact candidate digest and
workspace. Generated files still require separate real-runtime activation tests.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import psutil
from runtime import learned_install
from runtime.recipe_validation import validate


def prepare(recipe: dict, evidence_dir: Path, target: dict, workspace: Path) -> dict:
    checked = validate(recipe, evidence_dir, target=target)
    hook = recipe['hook']
    if hook.get('method') != 'file_plan':
        raise ValueError('candidate does not declare an executable file plan')
    root = Path(workspace).resolve(strict=True)
    claimed = hook.get('workspace')
    if not isinstance(claimed, str) or not Path(claimed).is_absolute() or Path(claimed).resolve() != root:
        raise ValueError('candidate workspace is missing or differs from supervisor scope')
    plan = learned_install.validate_plan(recipe.get('install_plan'))
    content = json.dumps(recipe,sort_keys=True,ensure_ascii=False).encode('utf-8')
    return {'status': 'candidate_prepared', 'workspace': str(root), 'target': dict(target),
            'plan': plan, 'plan_digest': learned_install.plan_digest(plan),
            'candidate_digest': hashlib.sha256(content).hexdigest(),
            'evidence': checked['evidence'],
            'activation': 'unverified', 'blocking': 'not_implemented'}


def execute(prepared: dict, state_dir: Path, *, approved_workspace: Path,
            approved_candidate_digest: str) -> dict:
    if prepared.get('status') != 'candidate_prepared' or prepared.get('candidate_digest') != approved_candidate_digest:
        raise PermissionError('candidate approval mismatch')
    target = prepared['target']
    if abs(psutil.Process(int(target['pid'])).create_time() - float(target['create_time'])) >= .001:
        raise ValueError('target instance changed before installation')
    result = learned_install.install(prepared['plan'], Path(prepared['workspace']), state_dir,
                                     approved_workspace=approved_workspace,
                                     approved_digest=prepared['plan_digest'])
    return {**result, 'target': dict(target), 'candidate_digest': prepared['candidate_digest'],
            'source': 'supervisor_approved_candidate', 'activation': 'unverified'}
