"""Product-independent file-plan installer for the PoC.

Goose supplies file paths/content, not an adapter name. The caller validates the
recipe's evidence and approves the exact workspace and plan digest. This module
never starts a target or executes generated code. Installation != activation.
It is intended for isolated, supervisor-owned workspaces, not hostile filesystem
writers or system-wide deployment.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from runtime import matcher

MAX_BYTES = 512 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or plan.get('version') != 1:
        raise ValueError('unsupported learned file plan version')
    files = plan.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 16:
        raise ValueError('plan requires 1-16 file changes')
    normalized, names, total = [], set(), 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('invalid file change')
        name, content = item.get('path'), item.get('content')
        if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
            raise ValueError('invalid relative path')
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
            raise ValueError('file path must stay inside workspace')
        if name in names or any(name.startswith(n + '/') or n.startswith(name + '/') for n in names):
            raise ValueError('duplicate or overlapping file paths')
        if not isinstance(content, str):
            raise ValueError('file content must be text')
        content.encode('utf-8')
        total += len(content.encode('utf-8'))
        if total > MAX_BYTES:
            raise ValueError('plan content exceeds PoC limit')
        expected = item.get('expected_sha256')
        if expected is not None and not (isinstance(expected, str) and re.fullmatch(r'[a-f0-9]{64}', expected)):
            raise ValueError('expected_sha256 must be null (create only) or a SHA256 digest')
        normalized.append({'path': name, 'content': content, 'expected_sha256': expected})
        names.add(name)
    return {'version': 1, 'files': normalized}


def plan_digest(plan: dict) -> str:
    return digest(json.dumps(validate_plan(plan), sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode('utf-8'))


def _root(path: Path) -> Path:
    # Reject explicit symlink components; macOS /var aliases may be resolved by
    # the supervisor before passing the approved workspace.
    path = Path(os.path.abspath(path))
    for p in [path, *path.parents]:
        if p.is_symlink():
            raise ValueError('symlink in supervisor path')
    if not path.is_dir():
        raise ValueError('supervisor workspace/state directory must already exist')
    return path


def _file(root: Path, name: str) -> Path:
    path = root.joinpath(*PurePosixPath(name).parts)
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError('symlink in installation path')
        current = current.parent
    if path.exists() and not path.is_file():
        raise ValueError('installation path is not a regular file')
    return path


def _read(path: Path) -> bytes | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('existing file too large')
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd, tmp = tempfile.mkstemp(prefix='.asg-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode)
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _save(path: Path, value: dict) -> None:
    _atomic(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


def install(plan: dict, workspace: Path, state_dir: Path, *,
            approved_workspace: Path, approved_digest: str) -> dict:
    """Execute an explicitly approved file plan, with full preflight and backups."""
    plan = validate_plan(plan)
    pid = plan_digest(plan)
    ws, state = _root(workspace), _root(state_dir)
    if ws != _root(approved_workspace) or pid != approved_digest:
        raise PermissionError('workspace or plan does not match approval')
    # State belongs to the supervisor, never to the model-generated file set.
    if state == ws or ws in state.parents:
        raise ValueError('installer state must be outside the target workspace')
    manifest = state / (pid + '.json')
    with matcher._THREAD_LOCK, matcher._FileLock(manifest):
        if manifest.is_symlink():
            raise ValueError('manifest symlink')
        prior = json.loads(manifest.read_text()) if manifest.exists() else None
        if prior and prior.get('workspace') != str(ws):
            raise ValueError('state belongs to another workspace')
        if prior and prior.get('status') == 'installed':
            if all(digest(_read(_file(ws, r['path'])) or b'') == r['after_sha256']
                   and _file(ws, r['path']).exists() for r in prior['changes']):
                return {'status': 'already_installed', 'plan_digest': pid, 'manifest': str(manifest),
                        'activation': 'unverified'}
            raise ValueError('installed files changed; refusing overwrite')
        if prior and prior.get('status') not in ('rolled_back',):
            raise ValueError('previous transaction requires review')
        changes = []
        for item in plan['files']:
            path = _file(ws, item['path']); before = _read(path)
            if (None if before is None else digest(before)) != item['expected_sha256']:
                raise ValueError('precondition changed: ' + item['path'])
            changes.append({'path': item['path'], 'before': None if before is None else base64.b64encode(before).decode(),
                            'before_mode': (path.stat().st_mode & 0o777) if before is not None else None,
                            'after_sha256': digest(item['content'].encode('utf-8'))})
        tx = {'version': 1, 'workspace': str(ws), 'plan_digest': pid, 'status': 'applying',
              'changes': changes, 'written': [], 'created_dirs': []}
        _save(manifest, tx)
        try:
            for item in plan['files']:
                path = _file(ws, item['path'])
                missing = []; parent = path.parent
                while parent != ws and not parent.exists():
                    missing.append(parent); parent = parent.parent
                for parent in reversed(missing):
                    parent.mkdir(mode=0o700); tx['created_dirs'].append(str(parent.relative_to(ws)))
                mode = next(c['before_mode'] for c in changes if c['path'] == item['path']) or 0o600
                _atomic(path, item['content'].encode('utf-8'), mode)
                tx['written'].append(item['path']); _save(manifest, tx)
            tx['status'] = 'installed'; _save(manifest, tx)
        except Exception:
            # Do not revert files this transaction never reached.
            _restore(ws, tx, only_written=True)
            tx['status'] = 'rolled_back'; _save(manifest, tx)
            raise
        return {'status': 'installed', 'plan_digest': pid, 'manifest': str(manifest),
                'activation': 'unverified'}


def _restore(ws: Path, tx: dict, only_written: bool = False) -> None:
    changes = [c for c in tx['changes'] if not only_written or c['path'] in tx['written']]
    for item in changes:
        path = _file(ws, item['path']); current = _read(path)
        if current is None or digest(current) != item['after_sha256']:
            raise ValueError('modified file blocks rollback: ' + item['path'])
    for item in reversed(changes):
        path = _file(ws, item['path'])
        if item['before'] is None:
            path.unlink()
        else:
            _atomic(path, base64.b64decode(item['before']), item['before_mode'])
    for name in reversed(tx.get('created_dirs', [])):
        try:
            (ws / name).rmdir()
        except OSError:
            pass  # Preserve directories containing unrelated user files.


def rollback(workspace: Path, state_dir: Path, *, approved_workspace: Path, approved_digest: str) -> dict:
    ws, state = _root(workspace), _root(state_dir)
    if ws != _root(approved_workspace) or not re.fullmatch(r'[a-f0-9]{64}', approved_digest):
        raise PermissionError('invalid rollback approval')
    manifest = state / (approved_digest + '.json')
    with matcher._THREAD_LOCK, matcher._FileLock(manifest):
        if manifest.is_symlink():
            raise ValueError('manifest symlink')
        tx = json.loads(manifest.read_text())
        if tx.get('workspace') != str(ws) or tx.get('plan_digest') != approved_digest:
            raise ValueError('rollback state mismatch')
        if tx['status'] == 'rolled_back':
            return {'status': 'already_rolled_back'}
        if tx['status'] != 'installed':
            raise ValueError('incomplete transaction requires review')
        _restore(ws, tx)
        tx['status'] = 'rolled_back'; _save(manifest, tx)
        return {'status': 'rolled_back', 'plan_digest': approved_digest}
