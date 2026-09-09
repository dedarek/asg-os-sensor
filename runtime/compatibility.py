"""Conservative observed build compatibility; native executables need no script token."""
import hashlib
import json
import platform
from pathlib import Path
from functools import lru_cache


@lru_cache(maxsize=128)
def _digest(path, size, mtime_ns, ctime_ns):
    if size > 512 * 1024 * 1024:
        return None
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def digest(path):
    try:
        p = Path(path).resolve(); s = p.stat()
        return _digest(str(p), s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    except (OSError, ValueError):
        return None


def observe(exe, argv, cwd):
    p = Path(exe)
    runtime = 'native'
    name = p.name.lower().removesuffix('.exe')
    if name in ('node', 'bun') or name.startswith('python'):
        runtime = 'python' if name.startswith('python') else name
    entry = None
    if runtime != 'native':
        # Unknown interpreter option syntax fails closed instead of guessing prompt text.
        if len(argv) > 1 and not argv[1].startswith('-'):
            entry = Path(argv[1]); entry = entry if entry.is_absolute() else Path(cwd) / entry
        else:
            return None
    build = {'executable': digest(exe), 'entry': digest(entry) if entry else 'native',
             'platform': platform.system(), 'architecture': platform.machine(), 'runtime': runtime}
    if not build['executable'] or not build['entry']:
        return None
    bundle = next((q for q in p.parents if q.suffix == '.app'), None)
    if bundle:
        for rel in ('Contents/Info.plist', 'Contents/Resources/app.asar'):
            file = bundle / rel
            if file.exists():
                build[rel] = digest(file)
                if not build[rel]: return None
    build['launch'] = hashlib.sha256(json.dumps([str(p.resolve()), argv[1:], cwd], sort_keys=True).encode()).hexdigest()
    build['entry_path'] = str(entry.resolve()) if entry else str(p.resolve())
    return build
