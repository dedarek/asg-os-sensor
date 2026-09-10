"""Read-only, bounded activity projection. Never return model text or raw evidence."""
from __future__ import annotations
import json
import os
import re
import stat
from pathlib import Path

MAX_BYTES = 256 * 1024


def _read(path: Path, tail: bool = False) -> tuple[bytes, bool]:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('not_regular_file')
        truncated = info.st_size > MAX_BYTES
        if truncated and not tail:
            raise ValueError('metadata_too_large')
        if truncated:
            stream.seek(-MAX_BYTES, os.SEEK_END)
        data = stream.read(MAX_BYTES)
        if truncated:
            data = data.partition(b'\n')[2]
        return data, truncated


def _json(path: Path) -> dict:
    value = json.loads(_read(path)[0])
    if not isinstance(value, dict):
        raise ValueError('invalid_metadata')
    return value


def _bound(value: dict, pid: int, create_time: float) -> bool:
    target = value.get('target', value)
    try:
        return int(target['pid']) == pid and abs(float(target['create_time']) - create_time) < .001
    except (KeyError, TypeError, ValueError):
        return False


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r'[\w.:-]{1,120}', value) else None


def _timestamp(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9TZ:+. -]{1,40}', value) else None


def snapshot(root: Path, pid: int, create_time: float, run_id: str | None = None, limit: int = 40) -> dict:
    """Only exact-instance run directories; request-supplied paths are never opened."""
    root = root.resolve()
    pid, create_time = int(pid), float(create_time)
    limit = max(1, min(100, int(limit)))
    if run_id and not re.fullmatch(r'pid_' + str(pid) + r'_[0-9]+', run_id):
        raise ValueError('invalid_run_id')
    dirs = [p for p in root.glob(f'pid_{pid}_*') if not p.is_symlink() and p.is_dir()]
    dirs.sort(key=lambda p: p.name.rsplit('_', 1)[-1].zfill(20), reverse=True)
    runs = []
    records = {}
    for path in dirs[:64]:
        lifecycle = {}
        binding = False
        for name in ('investigation_lifecycle.json', 'investigation_findings.json', 'result.json'):
            try:
                value = _json(path / name)
            except (OSError, ValueError):
                continue
            if _bound(value, pid, create_time):
                binding = True
                if name == 'investigation_lifecycle.json':
                    lifecycle = value
                break
        if not binding:
            continue
        runs.append({'run_id': path.name, 'status': _label(lifecycle.get('status')) or 'recorded',
                     'started_at': _timestamp(lifecycle.get('started_at'))})
        records[path.name] = (path, lifecycle)
    selected = run_id or (runs[0]['run_id'] if runs else None)
    if selected and selected not in records:
        raise LookupError('run_not_found_for_instance')
    result = {'target': {'pid': pid, 'create_time': create_time}, 'runs': runs,
              'run_id': selected, 'events': [], 'truncated': False,
              'limitations': ['Tool audit records completion, not start; duration is unavailable.',
                              'No model thinking, raw configuration, arguments or credential values are exposed.']}
    if not selected:
        result['status'] = 'not_started'
        return result
    path, lifecycle = records[selected]
    progress = lifecycle.get('progress') or {}
    result.update(status=_label(lifecycle.get('status')) or 'recorded',
                  started_at=_timestamp(lifecycle.get('started_at')),
                  ended_at=_timestamp(lifecycle.get('ended_at')),
                  last_activity_at=_timestamp(progress.get('last_activity_at')))
    events = []
    try:
        data, result['truncated'] = _read(path / 'analyst_tool_calls.jsonl', tail=True)
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue  # concurrent partial last line is not a completed event
            if not isinstance(row, dict):
                continue
            tool = _label(row.get('tool'))
            if not tool:
                continue
            events.append({'type': 'tool_completed', 'tool': tool, 'ts': _timestamp(row.get('ts')),
                           'status': 'failed' if row.get('error') else 'succeeded',
                           'evidence_id': _label(row.get('evidence_id')), 'duration_ms': None})
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        result['audit_status'] = 'unreadable'
    try:
        findings = _json(path / 'investigation_findings.json')
        if _bound(findings, pid, create_time):
            for row in (findings.get('history') or [])[-64:]:
                if isinstance(row, dict):
                    events.append({'type': 'finding_saved', 'ts': _timestamp(row.get('submitted_at')),
                                   'kind': _label(row.get('kind')), 'asset': _label(row.get('asset')),
                                   'status': _label(row.get('status'))})
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        result['findings_status'] = 'unreadable'
    events.sort(key=lambda row: row.get('ts') or '')
    result['truncated'] = result['truncated'] or len(events) > limit
    result['events'] = events[-limit:]
    return result
