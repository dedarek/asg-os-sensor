"""Short retention of instance-bound discovery evidence across scan gaps.

Agents that only touch the network while reasoning (bursty transports) can
miss a scheduled scan. The evidence was collected from this exact instance
(pid plus start time), so briefly re-presenting it does not invent anything;
the marker keeps the distinction auditable. Identity mismatch on PID reuse
discards the entry immediately.
"""
import time

DEFAULT_TTL_SECONDS = 300


def apply(cache: dict, pid, create_time, discovery, *, now=None, ttl=DEFAULT_TTL_SECONDS):
    """Return the discovery evidence to use for this snapshot.

    Fresh evidence refreshes the cache. With no fresh evidence, an unexpired
    entry for the same instance identity is returned with a retention marker;
    otherwise the empty result stands.
    """
    now = time.time() if now is None else now
    prior = cache.get(pid)
    if prior and abs(float(prior['create_time']) - float(create_time)) > 1e-3:
        del cache[pid]  # PID reuse: never carry evidence across instances.
        prior = None
    if discovery:
        cache[pid] = {'create_time': float(create_time), 'evidence': discovery,
                      'until': now + ttl}
        return discovery
    if prior and prior['until'] > now:
        retained = dict(prior['evidence'])
        retained['retained_for'] = 'recent-instance-evidence'
        return retained
    return {}


def prune(cache, *, now=None):
    now = time.time() if now is None else now
    for pid in [pid for pid, entry in cache.items() if entry['until'] <= now]:
        del cache[pid]
