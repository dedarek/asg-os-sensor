"""Discover reusable protocol evidence before spending a Goose investigation.

This module never mutates an Agent.  Installation is owned by the SOC package
pipeline, which adds the direct-SOC transport, compatibility constraints,
transactional rollback and fresh-instance verification.  A matching JSON
shape remains a candidate; only an opened configuration is loader evidence.
"""

import psutil

from runtime import autonomous_pipeline as pipeline, onboarding


def attempt(target, *, force=False):
    """Discover protocol candidates; installation belongs to the SOC package flow.

    A matching JSON shape is not loader or dialect compatibility evidence.
    Legacy local runtimes must never suppress investigation or claim SOC direct
    readiness, even when they have produced a real local callback.
    """
    iid = onboarding.make_instance_id(target['pid'], target['create_time'])
    prior = pipeline.read(iid).get('protocol_fastpath') or {}
    try:
        from runtime.protocol_discovery import discover
        process = psutil.Process(target['pid'])
        if abs(process.create_time() - target['create_time']) >= .001:
            raise ValueError('target changed')
        surface = discover(process)
        result = {'status': 'fallback', 'handled': False, 'verified': False,
                  'route': 'soc_package_pipeline',
                  'reason': '协议线索交给调查与 SOC 制品流程；确认加载位置和兼容性后生成直连安装包',
                  'protocol_discovery': surface,
                  'candidate_count': len(surface.get('candidates', []))}
        if prior.get('status') in ('installed', 'already_installed'):
            result['legacy_local_install'] = {k: prior[k] for k in
                ('state_dir', 'observe_config', 'installed_at', 'workspace') if k in prior}
            result['missing_checks'] = ['soc_direct_transport', 'soc_package_registration',
                                        'fresh_instance_callback']
        elif prior.get('legacy_local_install'):
            result['legacy_local_install'] = prior['legacy_local_install']
            result['missing_checks'] = prior.get('missing_checks', [])
    except (OSError, ValueError, KeyError, psutil.Error) as exc:
        result = {'status': 'fallback', 'handled': False, 'verified': False, 'reason': str(exc)}
    pipeline.save(iid, 'protocol_fastpath', '', **result)
    return result
