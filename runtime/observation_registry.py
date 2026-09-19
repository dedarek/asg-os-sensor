"""Persistent independent observation bindings, including immutable historical bindings."""
from __future__ import annotations
import json
import threading
import re
from pathlib import Path
from runtime import observation_source
from runtime.learned_install import _atomic
from runtime.file_lock import _FileLock

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

    def register(self, config_path, target=None, *, source_name=None, make_primary=False):
        config = observation_source.load_config(config_path)
        if target is not None and config['target'] != target:
            raise ValueError('observation target mismatch')
        target = config['target']
        iid = '%s:%s' % (target['pid'], target['create_time'])
        if source_name is not None and not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', source_name):
            raise ValueError('invalid observation source name')
        record_id = iid if source_name is None else iid + '--' + source_name
        # Copy the binding: a coordinator may later update its original file for
        # a new process. Historical instances must retain their original identity.
        with _LOCK, _FileLock(self.path):
            data = self._read()
            if make_primary and iid not in data: record_id = iid
            binding = self.root / 'observation-bindings' / (record_id.replace(':', '_') + '.json')
            binding.parent.mkdir(parents=True, exist_ok=True)
            raw = json.loads(Path(config_path).read_text())
            _atomic(binding, json.dumps(raw, ensure_ascii=False).encode())
            data[record_id] = {'config_path': str(binding), 'target': target,
                         'source_config': str(config_path)}
            _atomic(self.path, json.dumps(data, ensure_ascii=False).encode())
        return iid

    def snapshots(self, instance_ids=None):
        """Read bound observations, optionally limited to current instances.

        A registry deliberately retains historical bindings.  Dashboard polls
        only need the processes shown on the current page, so opening every old
        event log on each refresh made the UI stall as history accumulated.
        """
        with _LOCK, _FileLock(self.path):
            data = self._read()
        wanted = set(instance_ids) if instance_ids is not None else None
        result = {}
        for iid, record in data.items():
            base_iid = iid.split('--', 1)[0]
            if wanted is not None and base_iid not in wanted and iid not in wanted:
                continue
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
