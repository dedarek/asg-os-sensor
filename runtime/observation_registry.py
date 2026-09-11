"""Persistent independent observation bindings, including immutable historical bindings."""
from __future__ import annotations
import json
import threading
from pathlib import Path
from runtime import observation_source
from runtime.learned_install import _atomic

_LOCK = threading.RLock()

class Registry:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'observations.json'

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def register(self, config_path, target=None):
        config = observation_source.load_config(config_path)
        if target is not None and config['target'] != target:
            raise ValueError('observation target mismatch')
        target = config['target']
        iid = '%s:%s' % (target['pid'], target['create_time'])
        # Copy the binding: a coordinator may later update its original file for
        # a new process. Historical instances must retain their original identity.
        with _LOCK:
            data = self._read()
            binding = self.root / 'observation-bindings' / (iid + '.json')
            binding.parent.mkdir(parents=True, exist_ok=True)
            raw = json.loads(Path(config_path).read_text())
            _atomic(binding, json.dumps(raw, ensure_ascii=False).encode())
            data[iid] = {'config_path': str(binding), 'target': target,
                         'source_config': str(config_path)}
            _atomic(self.path, json.dumps(data, ensure_ascii=False).encode())
        return iid

    def snapshots(self):
        with _LOCK:
            data = self._read()
        result = {}
        for iid, record in data.items():
            try:
                config = observation_source.load_config(record['config_path'])
                if config['target'] != record['target']:
                    raise ValueError('stored observation target mismatch')
                result[iid] = observation_source.snapshot(config)
            except (ValueError, OSError) as exc:
                result[iid] = {'status': 'unavailable', 'message': str(exc),
                               'instance_pid': record['target']['pid'],
                               'instance_create_time': record['target']['create_time']}
        return result
