import copy
import unittest
from runtime.io_acceptance import assess, KINDS


class IOAcceptanceTests(unittest.TestCase):
    def fixture(self):
        rows = []
        for n, kind in enumerate(KINDS, 1):
            rows.append({'instance_id': '123:456', 'event_type': kind, 'payload': {
                'session_id': 's', 'turn_id': 't', 'event_id': str(n), 'sequence': n,
                'request_id': 'r', 'tool_call_id': 'c', 'content': {'text': 'actual'}, 'content_complete': True}})
        rows.append({'instance_id': '123:456', 'event_type': 'io.turn.end', 'payload': {
            'session_id': 's', 'turn_id': 't', 'last_sequence': 6, 'expected_counts': dict.fromkeys(KINDS, 1)}})
        return rows

    def test_complete_reported_turn_is_not_global_loss_proof(self):
        result = assess(self.fixture(), {})
        self.assertEqual(result['status'], 'checkpoint_verified')
        self.assertFalse(result['end_to_end_verified'])

    def test_missing_response_duplicate_retry_and_partial_stream_fail(self):
        rows = self.fixture()
        for altered in (rows[:2] + rows[3:], rows + [copy.deepcopy(rows[1])]):
            self.assertEqual(assess(altered, {})['status'], 'gaps')
        rows[2]['payload']['content_complete'] = False
        self.assertIn('missing_or_incomplete_content', assess(rows, {})['turns'][0]['gaps'])

    def test_other_instance_session_agent_cannot_complete_a_pair(self):
        for mutation in ('instance_id', 'session_id', 'agent_id', 'request_id'):
            rows = self.fixture()
            if mutation == 'instance_id':
                rows[2][mutation] = '123:999'
            else:
                rows[2]['payload'][mutation] = 'other'
            self.assertEqual(assess(rows, {})['status'], 'gaps')

    def test_otel_ids_and_empty_tool_result_supported(self):
        rows = self.fixture()
        for row in rows:
            p = row['payload']
            p['attributes'] = {'gen_ai.conversation.id': p.pop('session_id')}
            if 'tool_call_id' in p:
                p['attributes']['gen_ai.tool.call.id'] = p.pop('tool_call_id')
        rows[4]['payload']['content'] = ''
        self.assertEqual(assess(rows, {})['status'], 'checkpoint_verified')

    def test_truncated_window_and_missing_checkpoint_never_pass(self):
        self.assertEqual(assess(self.fixture(), {'source': {'truncated': True}})['status'], 'gaps')
        self.assertEqual(assess(self.fixture()[:-1], {})['status'], 'gaps')
        self.assertEqual(assess([], {})['status'], 'not_observed')

    def test_turn_without_tools_requires_explicit_zero_counts(self):
        rows = [r for r in self.fixture() if not r['event_type'].startswith('tool.')]
        for i, r in enumerate(rows[:-1], 1):
            r['payload']['sequence'] = i
        cp = rows[-1]['payload']
        cp['last_sequence'] = 4
        cp['expected_counts']['tool.execute.before'] = 0
        cp['expected_counts']['tool.execute.after'] = 0
        self.assertEqual(assess(rows, {})['status'], 'checkpoint_verified')
