"""Independent PoC verification of a generated hook's generic event contract.

This checks fresh instance-bound observations, not complete coverage or blocking.
It neither knows a product nor invokes hook callbacks to manufacture evidence.
"""
from __future__ import annotations
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
import psutil


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Accept epoch seconds or milliseconds, as declared by the generic producer.
        return float(value) / 1000 if value > 1e11 else float(value)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timestamp must include a timezone')
        return parsed.timestamp()
    raise ValueError('missing event timestamp')


def verify(path: Path, target: dict, *, nonce: str, not_before: float) -> dict:
    """Read a bounded local event file; never return the nonce or raw arguments."""
    if not nonce:
        raise ValueError('fresh run nonce required')
    pid, ct = int(target['pid']), float(target['create_time'])
    proc = psutil.Process(pid)
    if abs(proc.create_time() - ct) >= .001:
        raise ValueError('target instance changed')
    result = {'target': dict(target), 'status': 'awaiting_events', 'hook_loaded': False,
              'observing': False, 'valid_events': 0, 'invalid_events': 0,
              'paired_calls': [], 'events': [], 'coverage': ['declared tool callbacks only'],
              'blocking': 'not_implemented'}
    if not path.exists():
        return result
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('event file exceeds PoC scope')
    pending = {}
    clock_now = datetime.now(timezone.utc).timestamp()
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                event = json.loads(line)
                if event.get('pid') != pid or not hmac.compare_digest(str(event.get('nonce', '')), nonce):
                    raise ValueError('wrong instance')
                ts = _timestamp(event.get('timestamp'))
                if not max(ct, not_before) - 1 <= ts <= clock_now + 30:
                    raise ValueError('stale or future event')
                kind = event.get('event_type')
                if kind not in ('hook.loaded', 'tool.execute.before', 'tool.execute.after'):
                    raise ValueError('unknown event')
                if kind == 'hook.loaded':
                    result['hook_loaded'] = True
                else:
                    call_id, tool = event.get('call_id'), event.get('tool_name')
                    if not result['hook_loaded'] or not isinstance(call_id, str) or not call_id or not isinstance(tool, str) or not tool:
                        raise ValueError('activation or correlation missing')
                    if kind == 'tool.execute.before':
                        pending[call_id] = (tool, ts)
                    else:
                        if call_id not in pending or pending[call_id][0] != tool or ts < pending[call_id][1]:
                            raise ValueError('unpaired event')
                        pending.pop(call_id)
                        result['paired_calls'].append({'call_id': call_id[:120], 'tool_name': tool[:120]})
                result['valid_events'] += 1
                result['events'].append({'event_type': kind, 'timestamp': ts, 'pid': pid})
                result['events'] = result['events'][-30:]
            except (ValueError, TypeError, AttributeError):
                result['invalid_events'] += 1
    result['observing'] = bool(result['paired_calls'])
    result['status'] = 'observing' if result['observing'] else ('loaded' if result['hook_loaded'] else 'awaiting_events')
    return result
