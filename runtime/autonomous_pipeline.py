"""Durable per-instance phase progress; asset collection never completes Hook work."""
from __future__ import annotations
import json
import os
import threading
from pathlib import Path
from runtime.learned_install import _atomic

_LOCK = threading.RLock()

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
        path().parent.mkdir(parents=True, exist_ok=True)
        _atomic(path(), json.dumps(data, ensure_ascii=False).encode())

def next_phase(instance_id, exact=False):
    state = read(instance_id)
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
        return None
    if status == 'pending_authorization':
        return None  # Scanner retries execution when deployment scope changes.
    return 'hook'
