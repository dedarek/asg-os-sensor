"""Bridge an evidence-bound Goose candidate to the generic file installer.

No product registry or application template is consulted. This is an explicit
PoC execution path: the supervisor must approve the exact candidate digest and
workspace. Generated files still require separate real-runtime activation tests.
"""
from __future__ import annotations
import hashlib
import math
import json
from datetime import datetime, timezone
from pathlib import Path
import psutil
from runtime import learned_install
from runtime import matcher
from runtime import observation_source
from runtime.recipe_validation import validate

OBSERVATION_FILE = 'observation_source.json'
ACTIVATION_HISTORY_FILE = 'activation_bindings.json'


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


def prepare(recipe: dict, evidence_dir: Path, target: dict, workspace: Path,
            compatibility: dict | None = None) -> dict:
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
            'compatibility': dict(compatibility) if isinstance(compatibility, dict) else None,
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
            'workspace': prepared.get('workspace'), 'compatibility': prepared.get('compatibility'),
            'observation': observation}


def _normalize_target(target: dict) -> dict:
    if not isinstance(target, dict) or target.get('pid') is None or target.get('create_time') is None:
        raise ValueError('target requires pid and create_time')
    try:
        pid = int(target['pid'])
        create_time = float(target['create_time'])
    except (TypeError, ValueError) as exc:
        raise ValueError('target pid/create_time must be numeric') from exc
    if pid <= 0 or not math.isfinite(create_time) or create_time <= 0:
        raise ValueError('target pid and create_time must be finite and positive')
    return {'pid': pid, 'create_time': create_time}


def _same_build(observed, recorded) -> tuple[bool, str]:
    """A restart of the same program, not an upgrade or a different program.

    Requires the observed executable bytes and entry identity to be identical to
    what was recorded at install time, plus the shared family-identity check for
    path/bundle consistency. Family-only similarity is deliberately not enough:
    that would let an upgrade silently inherit an existing activation binding.
    """
    if not isinstance(observed, dict) or not isinstance(recorded, dict):
        return False, '缺少可核对的构建兼容性快照'
    for field in ('executable', 'entry'):
        o, r = observed.get(field), recorded.get(field)
        if not o or not r:
            return False, '构建兼容性缺少 %s' % field
        if o != r:
            return False, '构建身份不一致（%s 变化），这不是同一构建的重启' % field
    if not matcher._family_identity_matches(observed, recorded):
        return False, '入口/包身份不一致，属于不同家族'
    return True, 'same-build'


def rebind_activation(prepared: dict, state_dir: Path, *, approved_workspace: Path,
                      new_target: dict, observed_compatibility, evidence, reason=None) -> dict:
    """Rebind an installed activation to a restarted instance of the same build.

    This never reinstalls and never edits the candidate or the install manifest.
    It rewrites only the installer observation binding and appends a migration
    record. A bare new pid is refused: the caller must supply a build snapshot
    that matches the one recorded at install time plus explicit evidence.
    """
    if not isinstance(prepared, dict) or not prepared.get('workspace'):
        raise ValueError('prepared install record required')
    recorded = prepared.get('compatibility')
    if not isinstance(recorded, dict) or not recorded.get('executable'):
        raise ValueError('no recorded build compatibility; refusing to rebind without evidence')
    root = Path(prepared['workspace']).resolve(strict=True)
    if Path(approved_workspace).resolve(strict=True) != root:
        raise PermissionError('rebind workspace differs from the approved install workspace')

    target = _normalize_target(new_target)
    previous_target = _normalize_target(prepared['target'])
    if abs(psutil.Process(target['pid']).create_time() - target['create_time']) >= .001:
        raise ValueError('new target instance changed before rebinding')

    valid_evidence = (isinstance(evidence, list) and bool(evidence)
                      and all(isinstance(item, str) and item for item in evidence))
    if not valid_evidence:
        raise ValueError('rebind requires explicit caller verification evidence, not a bare pid')
    same, detail = _same_build(observed_compatibility, recorded)
    if not same:
        raise ValueError('refusing activation rebind: ' + detail)

    config_path = Path(state_dir) / OBSERVATION_FILE
    if not config_path.is_file():
        raise ValueError('no observation binding to rebind')
    current = observation_source.load_config(config_path)
    history_path = Path(state_dir) / ACTIVATION_HISTORY_FILE
    history = {}
    if history_path.is_file():
        try:
            history = json.loads(history_path.read_text(encoding='utf-8'))
        except ValueError as exc:
            raise ValueError('activation history unreadable') from exc
    if not history_path.is_file():
        # Preserve the original install-time binding so the pre-restart target
        # record survives the migration.
        history = {'bindings': [{'kind': 'initial', 'target': dict(previous_target),
                                 'compatibility': recorded, 'bound_at': None}]}
    if not isinstance(history, dict) or not isinstance(history.get('bindings'), list):
        raise ValueError('activation history must contain a bindings list')
    previous_target = _normalize_target(current['target'])
    if target == previous_target:
        return {'status': 'already_bound', 'target': dict(target)}
    payload = {key: current[key] for key in
               ('version', 'mapping_mode', 'log_path', 'fields', 'event_names')}
    payload['target'] = dict(target)
    learned_install._atomic(config_path, json.dumps(payload, ensure_ascii=False, indent=2,
                                                    sort_keys=True).encode('utf-8'))
    reloaded = observation_source.load_config(config_path)
    for key in ('mapping_mode', 'log_path', 'fields', 'event_names'):
        if reloaded[key] != current[key]:
            raise ValueError('rebound observation config does not round-trip: ' + key)
    if reloaded['target'] != target:
        raise ValueError('rebound observation config did not adopt the new target')

    record = {'kind': 'activation_rebind', 'target': dict(target),
              'previous_target': dict(previous_target),
              'compatibility': dict(observed_compatibility),
              'evidence': list(evidence), 'reason': reason,
              'bound_at': datetime.now(timezone.utc).isoformat()}
    history.setdefault('bindings', []).append(record)
    learned_install._atomic(history_path, json.dumps(history, ensure_ascii=False, indent=2,
                                                     sort_keys=True).encode('utf-8'))
    return {'status': 'activation_rebound', 'workspace': str(root), 'target': dict(target),
            'previous_target': dict(previous_target), 'config_path': str(config_path),
            'history_path': str(history_path),
            'build': {'executable': observed_compatibility.get('executable'),
                      'entry': observed_compatibility.get('entry')},
            'evidence': list(evidence), 'reason': reason,
            'activation': 'unverified', 'blocking': 'not_implemented'}


AUTHORIZATION_KEYS = ('approved_workspace', 'approved_candidate_digest', 'install', 'rebind')


def authorization_scope(*, approved_workspace, approved_candidate_digest=None,
                        allow_install=False, allow_rebind=False) -> dict:
    """Explicit supervisor scope. Nothing is authorized unless it is named here.

    ``allow_install``/``allow_rebind`` default to False: an unauthorized call may
    read and plan but never writes files or moves a binding. Nothing in this
    module starts, restarts or stops a target process.
    """
    if approved_workspace is None:
        raise ValueError('authorization requires an approved workspace')
    if approved_candidate_digest is not None and (not isinstance(approved_candidate_digest, str)
                                                  or not approved_candidate_digest):
        raise ValueError('approved_candidate_digest must be a non-empty string when given')
    if not isinstance(allow_install, bool) or not isinstance(allow_rebind, bool):
        raise ValueError('allow_install/allow_rebind must be booleans')
    return {'approved_workspace': str(Path(approved_workspace).resolve(strict=True)),
            'approved_candidate_digest': approved_candidate_digest,
            'install': allow_install, 'rebind': allow_rebind}


def _check_authorization(authorization, workspace: Path, candidate_digest: str) -> dict:
    if not isinstance(authorization, dict):
        raise ValueError('authorization scope required')
    unknown = sorted(set(authorization) - set(AUTHORIZATION_KEYS))
    if unknown:
        raise ValueError('unknown authorization keys: ' + ', '.join(unknown))
    approved = authorization.get('approved_workspace')
    if not approved or Path(approved).resolve(strict=True) != workspace:
        raise PermissionError('candidate workspace differs from the approved scope')
    stamped = authorization.get('approved_candidate_digest')
    if stamped is not None and stamped != candidate_digest:
        raise PermissionError('candidate digest does not match the approved scope')
    for key in ('install', 'rebind'):
        if not isinstance(authorization.get(key), bool):
            raise ValueError('authorization.%s must be a boolean' % key)
    return authorization


def coordinate(recipe: dict, evidence_dir: Path, target: dict, *, workspace: Path, state_dir: Path,
               authorization: dict, observed_compatibility=None, evidence=None,
               reason=None) -> dict:
    """Single entry point: candidate -> prepare/execute -> activation binding.

    Returns a service-consumable result. It is idempotent when the plan is already
    installed, asks for a rebind when the bound instance restarted, and reports
    ``pending_authorization`` (writing nothing) when the scope does not allow the
    step. It never restarts or stops a target process, and knows no product.
    """
    workspace_root = Path(workspace).resolve(strict=True)
    candidate_digest = hashlib.sha256(json.dumps(recipe, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    scope = _check_authorization(authorization, workspace_root, candidate_digest)
    state = Path(state_dir)
    record_path = state / 'prepared_install.json'
    expected_plan = learned_install.plan_digest(recipe.get('install_plan'))
    manifest = state / (expected_plan + '.json')
    installed = False
    if manifest.exists():
        tx = json.loads(manifest.read_text())
        if tx.get('status') != 'installed' or tx.get('workspace') != str(workspace_root):
            raise ValueError('installation manifest is not an installed transaction for this workspace')
        for change in tx.get('changes', []):
            path = learned_install._file(workspace_root, change['path'])
            if not path.is_file() or learned_install.digest(path.read_bytes()) != change['after_sha256']:
                raise ValueError('installed hook files changed; refusing bound status')
        if not tx.get('changes') or not record_path.is_file():
            raise ValueError('missing original installation record; explicit migration required')
        prepared = json.loads(record_path.read_text())
        if prepared.get('candidate_digest') != candidate_digest or prepared.get('plan_digest') != expected_plan or prepared.get('workspace') != str(workspace_root):
            raise ValueError('original installation record does not match candidate/workspace')
        validate(recipe, evidence_dir, target=prepared['target'])
        installed = True
    else:
        prepared = prepare(recipe, evidence_dir, target, workspace_root,
                           compatibility=observed_compatibility)
    config_path = state / OBSERVATION_FILE
    declared = prepared['observation']['status'] == 'configured'

    # Everything a consumer needs to act, whether or not a write happened.
    base = {'workspace': str(workspace_root), 'target': dict(target),
            'plan_digest': prepared['plan_digest'], 'candidate_digest': prepared['candidate_digest'],
            'observation_declared': declared, 'installed': installed,
            'config_path': str(config_path) if (installed and declared) else None,
            'activation': 'unverified', 'blocking': 'not_implemented',
            'restarted_target': False, 'authorization': dict(scope)}

    if not installed:
        if not scope['install']:
            base.update(status='pending_authorization',
                        reason='安装未获授权；未写入任何文件',
                        required_scope={'install': True},
                        planned_plan_digest=prepared['plan_digest'])
            return base
        executed = execute(prepared, state_dir, approved_workspace=workspace_root,
                           approved_candidate_digest=scope['approved_candidate_digest']
                           or prepared['candidate_digest'])
        learned_install._atomic(record_path, json.dumps(prepared, ensure_ascii=False, indent=2).encode('utf-8'))
        observation = executed.get('observation') or {}
        base.update(installed=True, install_status=executed.get('status'),
                    config_path=observation.get('config_path'), observation=observation,
                    status='installed' if observation.get('status') == 'configured'
                           else 'installed_no_observation')
        return base

    # Already installed: never install again. Only the binding may move.
    observation = {'status': 'not_configured' if not declared else 'configured',
                   'config_path': str(config_path) if declared else None}
    if not declared or not config_path.is_file():
        base.update(status='installed_no_observation', observation=observation,
                    reason='已安装，但候选未声明 observation_source' if not declared
                           else '绑定文件缺失，需重新调查')
        return base

    bound = observation_source.load_config(config_path)['target']
    live_target = _normalize_target(target)
    if abs(psutil.Process(live_target['pid']).create_time() - live_target['create_time']) >= .001:
        raise ValueError('activation target is no longer the bound process')
    if _normalize_target(bound) == _normalize_target(target):
        base.update(status='bound', observation=observation,
                    reason='已安装且绑定实例一致；未做任何写入')
        return base
    base.update(status='rebind_required', observation=observation,
                bound_target=dict(bound), required_scope={'rebind': True},
                reason='已安装，但绑定实例已变化；需授权重绑')
    if not scope['rebind']:
        return base
    rebound = rebind_activation(prepared, state_dir, approved_workspace=workspace_root,
                                new_target=target, observed_compatibility=observed_compatibility,
                                evidence=evidence, reason=reason)
    base.update(status='rebound', previous_target=rebound.get('previous_target'),
                config_path=rebound.get('config_path'), history_path=rebound.get('history_path'))
    return base
