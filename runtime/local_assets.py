"""Model-free, instance-bound configuration refresh independent of Hook reuse."""
import threading
import time
import psutil
from runtime.collection import collect

_cache = {}
_pending = set()
_lock = threading.Lock()


def current(pid, created, workspace=None):
    key = (pid, created, workspace)
    with _lock:
        cached = _cache.get(key)
        if key not in _pending and (cached is None or time.time() - cached['checked_at'] > 60):
            _pending.add(key)
            threading.Thread(target=_refresh, args=(key,), daemon=True).start()
    return cached or {'status': 'checking', 'assets': {}}


def _refresh(key):
    result = {'status': 'failed', 'assets': {}, 'checked_at': time.time()}
    try:
        p = psutil.Process(key[0])
        if abs(p.create_time() - key[1]) > .001:
            raise ValueError('instance_changed')
        result.update(collect(p, workspace=key[2]), status='checked')
        if abs(p.create_time() - key[1]) > .001:
            raise ValueError('instance_changed')
    except (psutil.Error, OSError, ValueError) as exc:
        result = {'status': 'failed', 'assets': {}, 'checked_at': time.time(), 'reason': str(type(exc).__name__)}
    finally:
        with _lock:
            _cache[key] = result
            _pending.discard(key)
            # Retain a bounded cache; each new instance gets independent observations.
            if len(_cache) > 256:
                oldest = min(_cache, key=lambda k: _cache[k]['checked_at'])
                _cache.pop(oldest, None)
