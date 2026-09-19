"""Durable per-instance phase progress; asset collection never completes Hook work."""
from __future__ import annotations
import json
import os
import threading
from pathlib import Path
from runtime.learned_install import _atomic

_LOCK = threading.RLock()
_SELF_CHECK_CACHE = {}

def enabled():
    return os.environ.get('ASG_PIPELINE', '0') == '1'

def path():
    return Path(os.environ.get('ASG_RUN_DIR', 'artifacts/stage1/dashboard')) / 'pipeline.json'

def read(instance_id):
    with _LOCK:
        try:
            return json.loads(path().read_text()).get(instance_id, {})
        except FileNotFoundError:
            return {}

def save(instance_id, phase, run_dir, **details):
    with _LOCK:
        try:
            data = json.loads(path().read_text())
        except FileNotFoundError:
            data = {}
        data.setdefault(instance_id, {})[phase] = {'run_dir': str(run_dir), **details}
        if phase == 'hook': data[instance_id].pop('upgrade',None)
        path().parent.mkdir(parents=True, exist_ok=True)
        _atomic(path(), json.dumps(data, ensure_ascii=False).encode())

def next_phase(instance_id, exact=False):
    state = read(instance_id)
    if state.get('upgrade',{}).get('requested'): return 'hook'
    # Compatible recipes take the deterministic installer path. A new instance
    # keeps unknown assets until observed; it does not require another LLM census.
    from runtime import onboarding
    if exact:
        prior = onboarding.instance_state(instance_id) or {}
        install = prior.get('install') or {}
        if install.get('status') in ('failed', 'reuse_requires_validation', 'unsupported'):
            return 'hook'
        verification = state.get('verification') or {}
        if verification.get('status') == 'verification_failed':
            repair = state.get('repair') or {}
            import time
            if repair.get('attempts', 0) < 2 and time.time() - repair.get('at', 0) > 300:
                return 'hook'
        return None
    if not state.get('assets'):
        return 'assets'
    if state['assets'].get('infrastructure'):
        return None
    # A matching build/candidate is not a successful installation. Read the
    # persisted execution outcome so failed or uninstalled reuse keeps learning.
    from runtime import onboarding
    prior = onboarding.instance_state(instance_id) or {}
    install = prior.get('install') or {}
    status = install.get('status')
    if status in ('installed', 'bound', 'rebound', 'activation_rebound',
                  'installed_no_observation', 'installed_pending_activation', 'already_installed'):
        verification=verify_installed(instance_id)
        if verification['status'] in ('awaiting_binding','verification_failed'):
            import time
            retry=state.get('repair',{})
            if retry.get('attempts',0)<2 and time.time()-retry.get('at',0)>300:
                return 'hook'
        return None
    if status == 'pending_authorization':
        return None  # Scanner retries execution when deployment scope changes.
    return 'hook'


def verify_installed(instance_id):
    """Re-evaluate actual binding health; installation never implies activation."""
    from runtime import onboarding, observation_source
    prior=onboarding.instance_state(instance_id) or {}
    install=prior.get('install') or {}
    records_path=path().parent/'observations.json'
    try:records=json.loads(records_path.read_text())
    except (FileNotFoundError,ValueError):records={}
    binding=records.get(instance_id)
    result={'status':'awaiting_binding','observing':False,'control_verified':False,
            'reason':'已安装，但未建立实例观测绑定'}
    if binding:
        try:
            config=observation_source.load_config(binding['config_path'])
            snap=observation_source.snapshot(config)
            health=snap.get('health') or {}
            pairs=snap.get('paired_calls') or []
            alive=snap.get('target_alive')
            result={'status':'observing' if pairs and alive else 'loaded' if health.get('loaded_observed') and alive else 'awaiting_activation' if alive else 'target_exited',
                    'observing':bool(pairs and alive),'control_verified':False,
                    'reason':'真实工具调用前后事件已配对' if pairs and alive else '等待重启／新会话加载后产生真实活动' if alive else '目标实例已退出，等待新实例重新绑定',
                    'valid_events':(snap.get('events') or {}).get('valid',0),
                    'invalid_events':(snap.get('events') or {}).get('invalid',0)}
            if snap.get('status')=='unavailable':result.update(status='verification_failed',reason=snap.get('message','观测源不可用'))
        except (ValueError,OSError) as exc:result.update(status='verification_failed',reason=str(exc))
    from runtime.hook_acceptance import snapshots as acceptances
    result['control_verified']=any(v.get('current') and str(v['target']['pid'])+':'+str(v['target']['create_time'])==instance_id for v in acceptances())
    if binding:
        from runtime.hook_data import snapshot as hook_snapshot
        import time
        key = (str(path().resolve()), instance_id)
        cached = _SELF_CHECK_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 5:
            result['selfcheck'] = cached[1]
        else:
            data = hook_snapshot(path().parent, instance_id=instance_id, limit=2000, max_bytes=4*1024*1024)
            checks = (data.get('coverage') or {}).get('integration_selfchecks', [])
            result['selfcheck'] = checks[0] if checks else {'status': 'pending', 'next_checks': ['observation_binding']}
            if len(_SELF_CHECK_CACHE) > 256: _SELF_CHECK_CACHE.clear()
            _SELF_CHECK_CACHE[key] = (time.monotonic(), result['selfcheck'])
        result['control_verified'] = bool(result['selfcheck'].get('control', {}).get('blocking_verified'))
    previous=read(instance_id).get('verification',{})
    if any(previous.get(k)!=v for k,v in result.items()):save(instance_id,'verification','',**result)
    return result
