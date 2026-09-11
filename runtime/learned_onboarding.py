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
from runtime import observation_source
from runtime.recipe_validation import validate

OBSERVATION_FILE = 'observation_source.json'


def _prepare_observation(recipe: dict, root: Path, target: dict) -> dict:
    """Validate the candidate declaration and convert it into a source config.

    Absent declaration stays compatible and is reported explicitly as
    ``not_configured``; it never falls back to a built-in product answer. The
    instance binding always comes from the executor's ``target``.
    """
    declaration = recipe.get('observation_source')
    if declaration is None:
        return {'status': 'not_configured',
                'reason': '候选未声明 observation_source；不产生默认字段映射'}
    config = observation_source.candidate_to_config(declaration, root, target)
    return {'status': 'configured', 'config': config}


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
    observation = _prepare_observation(recipe, root, target)
    return {'status': 'candidate_prepared', 'workspace': str(root), 'target': dict(target),
            'plan': plan, 'plan_digest': learned_install.plan_digest(plan),
            'candidate_digest': hashlib.sha256(content).hexdigest(),
            'observation': observation,
            'evidence': checked['evidence'],
            'activation': 'unverified', 'blocking': 'not_implemented'}


def _persist_observation(prepared: dict, state_dir: Path) -> dict:
    """Write the bound config into the installer state dir and prove it reloads."""
    observation = prepared.get('observation') or {}
    if observation.get('status') != 'configured':
        return {'status': 'not_configured',
                'reason': observation.get('reason', '候选未声明 observation_source'),
                'config_path': None}
    config = observation['config']
    # The config lives with the install record, never inside the target files.
    path = Path(state_dir) / OBSERVATION_FILE
    payload = {key: config[key] for key in
               ('version', 'mapping_mode', 'target', 'log_path', 'fields', 'event_names')}
    learned_install._atomic(path, json.dumps(payload, ensure_ascii=False, indent=2,
                                             sort_keys=True).encode('utf-8'))
    # A saved config must reload to exactly the same mapping; otherwise the
    # consumer would silently read different field names than the candidate
    # declared. Any mismatch is a hard error, not a warning.
    reloaded = observation_source.load_config(path)
    for key in ('mapping_mode', 'log_path', 'fields', 'event_names', 'target'):
        if reloaded[key] != config[key]:
            raise ValueError('saved observation config does not round-trip: ' + key)
    return {'status': 'configured', 'config_path': str(path),
            'log_path': config['log_path'], 'mapping_mode': config['mapping_mode'],
            'fields': dict(config['fields'])}


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
    # Only a completed install may publish an observation binding, so a failed
    # install never leaves a config pointing at a log nothing writes.
    if result.get('status') not in ('installed', 'already_installed'):
        raise ValueError('install did not complete; observation config not written')
    observation = _persist_observation(prepared, state_dir)
    return {**result, 'target': dict(target), 'candidate_digest': prepared['candidate_digest'],
            'source': 'supervisor_approved_candidate', 'activation': 'unverified',
            'observation': observation}
