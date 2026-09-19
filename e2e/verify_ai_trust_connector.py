"""Local verification of the AI Trust connector (no platform available).

Uses an injected transport as a clearly-labelled test double to exercise the
connector: field mapping, credential handling, offline spooling, catch-up,
duplicate resend, correlation and latency measurement. Every result stays
platform_verified=False; the real platform sign-off remains an external
condition and is not claimed here.

Usage: python3 e2e/verify_ai_trust_connector.py
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime import ai_trust  # noqa: E402


class FakePlatform:
    """Injected transport (local test double). Never claims to be the platform."""

    def __init__(self):
        self.mode = 'ok'
        self.latency = 0.0
        self.calls = 0
        self.received = []
        self.seen = set()  # platform-side dedup by idempotency key
        self.new_total = 0

    def __call__(self, method, url, body):
        self.calls += 1
        if self.latency:
            time.sleep(self.latency)
        if self.mode == 'offline':
            raise OSError('simulated network down')
        if self.mode == 'bad_credentials':
            return 401, json.dumps({'error': 'invalid token'}).encode()
        payload = json.loads(body.decode())
        events = payload.get('events') or []
        ids = [e.get('event_id') for e in events if e.get('event_id')]
        # The platform dedups by event_id; a resend of an existing id records nothing new.
        new_ids = [i for i in ids if i not in self.seen]
        self.seen.update(new_ids)
        self.new_total += len(new_ids)
        self.received.extend(events)
        return 200, json.dumps({'recorded_event_ids': ids, 'new_event_ids': new_ids}).encode()


def make_events(n, instance='hostA:100'):
    events = []
    for i in range(n):
        events.append({'event_id': 'ev-%04d' % i, 'instance_id': instance,
                       'session_id': 'ses-%d' % (i % 3), 'turn_id': 'turn-%d' % i,
                       'agent_id': 'build', 'event_type': 'tool.execute.before',
                       'timestamp': 1789000000 + i, 'content': 'payload-%d' % i,
                       'content_complete': True, 'sequence': i + 1,
                       'tool': 'read', 'call_id': 'call-%d' % i,
                       'request_id': 'req-%d' % i})
    return events


def main():
    report = {'started_at': time.time()}
    # configuration: missing platform address / credential is reported, not faked
    report['missing_config'] = ai_trust.configured(ai_trust.load_config(environ={}))
    report['with_config'] = ai_trust.configured(ai_trust.load_config(
        environ={'ASG_AI_TRUST_URL': 'https://trust.example', 'ASG_AI_TRUST_TOKEN': 'tok'}))

    with tempfile.TemporaryDirectory() as tmp:
        spool_path = Path(tmp).resolve() / 'spool.jsonl'
        spool = ai_trust.Spool(spool_path)
        events = make_events(200)
        enqueued = [spool.enqueue(e) for e in events]
        duplicate_immediate = spool.enqueue(dict(events[0]))
        report['spool'] = {'enqueued': sum(1 for x in enqueued if x), 'size': spool.size(),
                           'immediate_duplicate_rejected': duplicate_immediate is False}
        # survives a restart
        reloaded = ai_trust.Spool(spool_path)
        report['spool']['survives_restart'] = reloaded.size()

        platform = FakePlatform()
        config = {'base_url': 'https://trust.example', 'token': 'tok', 'timeout': 5}

        # offline: nothing accepted, everything kept
        platform.mode = 'offline'
        offline = ai_trust.flush(reloaded, ai_trust.TrustClient(config, platform))
        report['offline'] = offline

        # bad credentials: rejected, nothing marked sent, no false success
        platform.mode = 'bad_credentials'
        bad = ai_trust.flush(reloaded, ai_trust.TrustClient(config, platform))
        report['bad_credentials'] = bad
        report['after_bad_credentials_size'] = reloaded.size()

        # recovery: catch up, measure latency
        platform.mode = 'ok'
        platform.latency = 0.02
        latencies = []
        client = ai_trust.TrustClient(config, platform)
        started = time.time()
        recovered = ai_trust.flush(reloaded, client)
        report['recovery'] = recovered
        report['catch_up_seconds'] = round(time.time() - started, 3)
        report['remaining_after_recovery'] = reloaded.size()
        report['accepted_payload_complete'] = all(
            e.get('payload_complete') is True for e in platform.received)

        # duplicate resend: the same idempotency keys must add zero logical records
        dups = [reloaded.enqueue(e) for e in events[:20]]
        report['duplicate_resend_enqueued'] = sum(1 for x in dups if x)
        before_new = platform.new_total
        dup_result = ai_trust.flush(reloaded, client)
        report['duplicate_resend'] = dup_result
        report['duplicate_resend_new_records'] = platform.new_total - before_new
        report['size_after_duplicate_resend'] = reloaded.size()

        # correlation: platform echo carries the local correlation id
        correlated = sum(1 for e in platform.received[:50]
                         if isinstance(e.get('correlation_id'), str) and e['correlation_id'].startswith('req-'))
        report['correlation_matched'] = correlated
        report['received_total'] = len(platform.received)

    gates = {
        'platform_config_requires_url_and_token': report['missing_config']['missing'] == ['base_url', 'token'],
        'field_mapping_complete': ai_trust.mapping_gaps() == [],
        'spool_persists_and_dedups': report['spool']['enqueued'] == 200
        and report['spool']['immediate_duplicate_rejected']
        and report['spool']['survives_restart'] == 200,
        'offline_events_kept': report['offline']['remaining'] == 200,
        'bad_credentials_no_false_success': report['bad_credentials']['status'] == 'rejected_credentials'
        and report['after_bad_credentials_size'] == 200,
        'catch_up_after_recovery_missing_0': report['recovery']['accepted'] == 200
        and report['remaining_after_recovery'] == 0,
        'duplicate_resend_zero_new_logical_records': report['duplicate_resend_new_records'] == 0
        and report['size_after_duplicate_resend'] == 0,
        'correlation_50_of_50': report['correlation_matched'] == 50,
        'never_claims_platform_verified': all(
            block.get('platform_verified') is False for block in
            (report['offline'], report['bad_credentials'], report['recovery'], report['missing_config'], report['with_config'])),
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    report['external_condition'] = '真实平台签收仍需 AI Trust 测试环境、接入协议与测试凭证'
    report['test_double_note'] = '本验证使用注入的本地测试替身，不代表平台侧验收'
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'ai-trust-connector.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'gates': gates, 'passed': report['passed']}, ensure_ascii=False))
    print('spool', json.dumps(report['spool'], ensure_ascii=False))
    print('offline', json.dumps(report['offline'], ensure_ascii=False))
    print('bad', json.dumps(report['bad_credentials'], ensure_ascii=False))
    print('recovery', json.dumps(report['recovery'], ensure_ascii=False), 'catch_up', report['catch_up_seconds'])


if __name__ == '__main__':
    main()
