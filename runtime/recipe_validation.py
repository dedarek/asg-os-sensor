"""Validate evidence references supplied by Goose, without executing any proposal.

结构校验只证明"配方形态合格、证据存在且成功"，不等于 Hook 建议有证据支持、更不等于
Hook 已安装或已验证。返回值明确区分二者:
- evidence: 通过校验的证据摘要 (每条均已绑定目标实例、结果非空)。
- hook_evidence_supported: hook.method 是否为本地证据可支持的接入点。
  当前没有可核对接入点存在的证据映射，因此恒为 False (proposed/unverified)；
  结构校验通过并不等于 Hook 建议有证据支持。
"""
import json
import re
from pathlib import Path

from runtime.learned_install import validate_plan

OBSERVATION_TOOLS = ('get_target_context', 'inspect_config_surface',
                     'observe_tree', 'observe_runtime_surface',
                     'inspect_network_peers', 'inspect_execution_trace',
                     'inspect_stream', 'inspect_loader_surface',
                     'inspect_observation', 'inspect_entry_surface',
                     'find_related_files', 'read_related_file')


def _real_success(item) -> bool:
    """结果必须是非空、可观察的真实成功 (允许局部 failed 子字段)。"""
    if item.get('error'):
        return False
    result = item.get('result')
    if result is None:
        return False
    if isinstance(result, dict):
        if not result:
            return False
        data_keys = [k for k, v in result.items()
                     if k not in ('status', 'message', 'reason', 'next')
                     and v not in (None, False, [], {})]
        return bool(data_keys)
    if isinstance(result, list):
        return bool(result)
    if isinstance(result, str):
        return bool(result.strip())
    return True


def validate(recipe, evidence_dir, target=None):
    """校验配方结构 + 证据质量 + 目标实例绑定。

    target: {'pid': int, 'create_time': float} 调查启动时冻结的实例身份。
    证据文件必须带 target 字段且与之完全一致; 证据缺失绑定或绑定不一致即拒绝
    (PID 复用 / 跨实例混用历史证据)。
    返回 {'evidence': [...], 'hook_evidence_supported': bool}。
    """
    if not isinstance(recipe, dict):
        raise ValueError('Recipe must be an object')
    for key, kind in {'agent_identity_name': str, 'match_features': dict, 'hook': dict,
                      'observation': str, 'fallback': str, 'evidence_refs': list}.items():
        if not isinstance(recipe.get(key), kind) or not recipe[key]:
            raise ValueError('Missing or invalid recipe field: ' + key)
    hook = recipe['hook']
    for field in ('method', 'restart_required', 'capabilities', 'verification', 'rollback', 'limitations'):
        if field not in hook:
            raise ValueError('Missing hook field: ' + field)
    if not isinstance(hook['method'], str) or not hook['method']:
        raise ValueError('Invalid hook method')
    if not (isinstance(hook['restart_required'], bool) or hook['restart_required'] == 'unknown'):
        raise ValueError('Invalid restart requirement')
    if not isinstance(hook['capabilities'], list) or not isinstance(hook['limitations'], list):
        raise ValueError('Hook capabilities/limitations must be lists')
    if not all(isinstance(hook[k], str) and hook[k] for k in ('verification', 'rollback')):
        raise ValueError('Hook verification/rollback descriptions required')
    install_plan = recipe.get('install_plan')
    if install_plan is not None:
        # Candidate file plans are validated here; execution still requires a
        # supervisor-approved workspace and plan digest (learned_install.install).
        # Nothing in this gate writes files or runs model-generated code.
        try:
            validate_plan(install_plan)
        except ValueError as exc:
            raise ValueError('Invalid install_plan: ' + str(exc)) from exc
        if hook['method'] not in ('file_plan', 'unsupported'):
            raise ValueError('install_plan requires hook.method="file_plan" or "unsupported"')

    refs = recipe['evidence_refs']
    evidence = []
    seen_target = None
    for ref in refs:
        if not isinstance(ref, str) or not re.fullmatch(r'ev-[0-9]+-[a-f0-9]{10}', ref):
            raise ValueError('Invalid evidence reference: ' + str(ref))
        base = Path(evidence_dir) / (ref + '.json')
        item = json.loads(base.read_text(encoding='utf-8'))
        if item.get('error') or item.get('tool') == 'propose_recipe':
            raise ValueError('Reference is not successful observation evidence: ' + ref)
        if item.get('tool') not in OBSERVATION_TOOLS:
            raise ValueError('Evidence from non-observation tool: ' + ref)
        if not _real_success(item):
            raise ValueError('Evidence has no real successful result: ' + ref)
        bound = item.get('target') or {}
        if not bound.get('pid') or bound.get('create_time') is None:
            raise ValueError('Evidence not bound to a target instance: ' + ref)
        cur = (int(bound.get('pid')), float(bound.get('create_time')))
        if seen_target is None:
            seen_target = cur
        elif cur != seen_target:
            raise ValueError('Evidence spans multiple target instances: ' + ref)
        evidence.append({'evidence_id': ref, 'tool': item['tool']})

    if not any(e['tool'] in ('get_target_context', 'inspect_config_surface',
                             'inspect_loader_surface') for e in evidence):
        raise ValueError('Bound target evidence required')
    if target is not None:
        exp = (int(target['pid']), float(target['create_time']))
        if seen_target != exp:
            raise ValueError('Evidence bound to a different instance than the investigation target')

    # hook evidence support cannot be inferred from a negative word list.
    # Evidence exists and is bound to the instance, but no evidence structure
    # verifies the hook.method entrypoint. Until an explicit mapping exists,
    # hook_evidence_supported is always False (proposed/unverified).
    hook_evidence_supported = False
    return {'evidence': evidence, 'hook_evidence_supported': hook_evidence_supported}
