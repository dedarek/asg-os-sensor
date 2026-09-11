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
    if state['assets'].get('infrastructure') or exact or state.get('hook'):
        return None
    return 'hook'
