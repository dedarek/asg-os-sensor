"""Connector invariants: mapping completeness, idempotent spool, honest failures."""
import json
import tempfile
import unittest
from pathlib import Path

from runtime import ai_trust


class _Transport:
    def __init__(self, mode='ok'):
        self.mode = mode
        self.seen = set()
        self.new_total = 0

    def __call__(self, method, url, body):
        if self.mode == 'offline':
            raise OSError('down')
        if self.mode == 'bad_credentials':
            return 401, b'{}'
        payload = json.loads(body.decode())
        ids = [e.get('event_id') for e in payload.get('events') or [] if e.get('event_id')]
        new_ids = [i for i in ids if i not in self.seen]
        self.seen.update(new_ids)
        self.new_total += len(new_ids)
        return 200, json.dumps({'recorded_event_ids': ids, 'new_event_ids': new_ids}).encode()


def _events(n, prefix='ev'):
    return [{'event_id': '%s-%d' % (prefix, i), 'instance_id': 'h:1', 'event_type': 'user.input',
             'timestamp': i, 'content': 'c%d' % i, 'request_id': 'r-%d' % i} for i in range(n)]


class AiTrustTests(unittest.TestCase):
    def test_mapping_is_complete_and_pure(self):
        self.assertEqual(ai_trust.mapping_gaps(), [])
        mapped = ai_trust.map_record({'event_id': 'e1', 'instance_id': 'h:1', 'event_type': 'user.input'})
        self.assertEqual(mapped['event_id'], 'e1')
        self.assertEqual(mapped['instance_ref'], 'h:1')
        self.assertEqual(mapped['event_kind'], 'user.input')

    def test_config_requires_url_and_token(self):
        self.assertFalse(ai_trust.configured(ai_trust.load_config(environ={}))['configuration_ready'])
        ready = ai_trust.configured(ai_trust.load_config(
            environ={'ASG_AI_TRUST_URL': 'u', 'ASG_AI_TRUST_TOKEN': 't'}))
        self.assertTrue(ready['configuration_ready'])
        self.assertFalse(ready['platform_verified'])

    def test_spool_persists_and_dedups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 's.jsonl'
            spool = ai_trust.Spool(path)
            for e in _events(5):
                self.assertTrue(spool.enqueue(e))
            self.assertFalse(spool.enqueue(_events(1)[0]))
            self.assertEqual(ai_trust.Spool(path).size(), 5)

    def test_offline_and_bad_credentials_keep_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = ai_trust.Spool(Path(tmp).resolve() / 's.jsonl')
            for e in _events(10):
                spool.enqueue(e)
            client = ai_trust.TrustClient({'base_url': 'u', 'token': 't'}, _Transport('offline'))
            result = ai_trust.flush(spool, client)
            self.assertEqual(result['remaining'], 10)
            self.assertFalse(result['platform_verified'])
            bad = ai_trust.TrustClient({'base_url': 'u', 'token': 't'}, _Transport('bad_credentials'))
            rejected = ai_trust.flush(spool, bad)
            self.assertEqual(rejected['status'], 'rejected_credentials')
            self.assertEqual(spool.size(), 10)

    def test_recovery_and_duplicate_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = ai_trust.Spool(Path(tmp).resolve() / 's.jsonl')
            events = _events(10)
            for e in events:
                spool.enqueue(e)
            transport = _Transport('ok')
            client = ai_trust.TrustClient({'base_url': 'u', 'token': 't'}, transport)
            self.assertEqual(ai_trust.flush(spool, client)['remaining'], 0)
            before = transport.new_total
            for e in events[:3]:
                spool.enqueue(e)
            ai_trust.flush(spool, client)
            self.assertEqual(transport.new_total - before, 0)
            self.assertEqual(spool.size(), 0)

    def test_no_acknowledgement_keeps_records(self):
        class NoAck(_Transport):
            def __call__(self, method, url, body):
                return 200, b'{}'
        with tempfile.TemporaryDirectory() as tmp:
            spool = ai_trust.Spool(Path(tmp).resolve() / 's.jsonl')
            spool.enqueue(_events(1)[0])
            result = ai_trust.flush(spool, ai_trust.TrustClient({'base_url': 'u', 'token': 't'}, NoAck()))
            self.assertEqual(result['status'], 'no_acknowledgement')
            self.assertEqual(spool.size(), 1)


if __name__ == '__main__':
    unittest.main()
