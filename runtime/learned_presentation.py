"""Pure projection from learned_install/learned_events results to dashboard state.

No product knowledge, no I/O. The caller supplies target identity, the
learned_install result (or None) and the learned_events.verify result (or None).
Returns hook_state for /api/state and a read-only summary for the UI.
"""
from __future__ import annotations
from typing import Any


def hook_state(target: dict[str, Any], install_result: dict[str, Any] | None,
               verify_result: dict[str, Any] | None) -> dict[str, Any]:
    """Derive hook_state + summary. Installation and verification are separate."""
    pid, ct = target.get('pid'), target.get('create_time')
    base = {'status': 'not_installed', 'label': '未安装', 'verified': False,
            'blocking': 'not_implemented', 'source': None,
            'target': {'pid': pid, 'create_time': ct}}
    if not isinstance(install_result, dict) or install_result.get('status') not in ('installed', 'already_installed'):
        return base
    base['status'] = 'installed_pending_activation'
    base['label'] = '已安装，待激活'
    base['source'] = install_result.get('source', 'learned_install')
    base['plan_digest'] = install_result.get('plan_digest')
    if verify_result is None or not isinstance(verify_result, dict):
        base['activation'] = 'awaiting_events'
        return base
    if verify_result.get('target') != base['target']:
        return base
    if not verify_result.get('hook_loaded'):
        base['activation'] = 'awaiting_events'
        return base
    base['status'] = 'loaded'
    base['label'] = '已加载（hook.loaded 观测）'
    base['verified'] = True
    base['activation'] = 'hook_loaded'
    base['valid_events'] = verify_result.get('valid_events', 0)
    base['invalid_events'] = verify_result.get('invalid_events', 0)
    if verify_result.get('observing') and verify_result.get('paired_calls'):
        base['status'] = 'observing'
        base['label'] = '已观测（before/after 配对）'
        base['observing'] = True
        base['paired_calls'] = verify_result['paired_calls']
    base['limitations'] = ['blocking is not implemented in Stage1',
                           'observations are instance-bound; reuse requires fresh verification']
    return base
