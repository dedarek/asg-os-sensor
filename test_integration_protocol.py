import copy
import json
import tempfile
import unittest
from pathlib import Path

from runtime import integration_protocol as protocol
from runtime.protocol_events import normalize
from runtime.integration_selfcheck import assess
from runtime.io_acceptance import KINDS


class ProtocolTests(unittest.TestCase):
    def test_structures_not_names_and_no_command_leak(self):
        data = {'name': 'unseen-product', 'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [
            {'type': 'command', 'command': 'private-command --credential secret'}]}]}}
        result = protocol.detect(data)
        self.assertEqual(result[0]['family'], 'command_hooks')
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(protocol.detect({'name': 'Claude Codex ACP', 'description': 'PreToolUse hooks'}), [])
        self.assertEqual(protocol.detect({'prior_memory': data, 'integration_protocol': data}), [])
        self.assertEqual(protocol.detect({'hooks': {'PreToolUse': [{'command': 'no type'}]}}), [])

    def test_acp_handshake_and_serialized_configuration(self):
        self.assertEqual(protocol.detect({'result': {'protocolVersion': 1, 'agentCapabilities': {}}})[0]['family'], 'acp')
        self.assertEqual(protocol.detect({'text': json.dumps({'hooks': {'Stop': [{'type': 'command', 'command': 'emit'}]}})})[0]['events'], ['Stop'])

    def test_protocol_selection_requires_bound_inspection_and_fallback_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = 'ev-123-0123456789'
            source_ref = 'ev-124-0123456789'
            target = {'pid': 42, 'create_time': 1.0}
            file = Path(tmp) / (ref + '.json')
            item = {'tool': 'inspect_integration_protocols', 'target': target,
                    'result': {'candidates': [{'family': 'command_hooks'}]}}
            file.write_text(json.dumps(item))
            (Path(tmp) / (source_ref + '.json')).write_text(json.dumps({
                'tool': 'read_related_file', 'target': target,
                'result': {'path': '/tmp/hooks.json', 'content': {'hooks': {}}}}))
            recipe = {'evidence_refs': [ref, source_ref], 'integration': {
                'family': 'command_hooks', 'evidence_refs': [ref, source_ref],
                'missing_capabilities': ['model.request']}}
            protocol.validate_selection(recipe, tmp, target, required=True)
            recipe['integration']['family'] = 'acp'
            with self.assertRaises(ValueError): protocol.validate_selection(recipe, tmp, target)
            recipe['integration']['family'] = 'plugin'
            with self.assertRaises(ValueError): protocol.validate_selection(recipe, tmp, target)
            recipe['integration']['fallback_reason'] = 'Observed hooks omit model transport; plugin wraps SDK'
            protocol.validate_selection(recipe, tmp, target)
            with self.assertRaises(ValueError): protocol.validate_selection(recipe, tmp, {'pid': 42, 'create_time': 2.0})
            with self.assertRaises(ValueError): protocol.validate_selection({}, tmp, target, required=True)
            self.assertIsNone(protocol.validate_selection({}, tmp, target))  # legacy remains readable

    def test_inspection_rejects_other_instance_and_generated_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = 'ev-123-0123456789'
            target = {'pid': 42, 'create_time': 1.0}
            item = {'target': target, 'tool': 'read_related_file', 'result': {'protocolVersion': 1, 'agentCapabilities': {}}}
            file = Path(tmp) / (ref + '.json')
            file.write_text(json.dumps(item))
            self.assertEqual(protocol.inspect(tmp, target, [ref])['candidates'][0]['family'], 'acp')
            with self.assertRaises(ValueError): protocol.inspect(tmp, {'pid': 42, 'create_time': 2.0}, [ref])
            item['tool'] = 'submit_investigation_finding'
            file.write_text(json.dumps(item))
            with self.assertRaises(ValueError): protocol.inspect(tmp, target, [ref])

    def test_mixed_evidence_recovers_without_relaxing_instance_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = {'pid': 42, 'create_time': 1.0}
            valid, contract, foreign = ['ev-%d-0123456789' % n for n in (1, 2, 3)]
            for ref, tool, bound in ((valid, 'inspect_config_surface', target),
                                     (contract, 'get_control_contract', target),
                                     (foreign, 'inspect_entry_surface', {'pid': 43, 'create_time': 2.0})):
                (Path(tmp)/(ref+'.json')).write_text(json.dumps({'target': bound, 'tool': tool,
                    'result': {'protocolVersion': 1, 'agentCapabilities': {}}}))
            result = protocol.inspect(tmp, target, [valid, contract, '../../bad'])
            self.assertEqual(result['sources'], [valid])
            self.assertEqual(len(result['skipped_evidence']), 2)
            self.assertEqual(result['candidates'][0]['family'], 'acp')
            self.assertEqual(protocol.inspect(tmp, target, None)['sources'], [valid])
            with self.assertRaisesRegex(ValueError, 'another target instance'):
                protocol.inspect(tmp, target, [valid, foreign])
            with self.assertRaisesRegex(ValueError, 'allowed_tools'):
                protocol.inspect(tmp, target, [contract])


class NormalizationTests(unittest.TestCase):
    def test_native_hook_preserves_missing_fields_and_raw(self):
        raw = {'instance_id': '42:1.0', 'payload': {'hook_event_name': 'PreToolUse', 'tool_input': {'command': 'echo x'}, 'session_id': 's', 'tool_use_id': 'native-call'}}
        saved = copy.deepcopy(raw)
        row = normalize(raw)
        self.assertEqual(raw, saved)
        self.assertEqual(row['event_type'], 'tool.execute.before')
        self.assertEqual(row['payload']['content'], {'command': 'echo x'})
        self.assertFalse(row['payload']['content_complete'])
        self.assertIsNone(row['payload']['turn_id'])
        self.assertEqual(row['payload']['attributes']['gen_ai.conversation.id'], 's')

    def test_acp_chunks_are_not_model_responses_or_control_proof(self):
        raw = {'instance_id': '42:1.0', 'payload': {'jsonrpc': '2.0', 'method': 'session/update',
               'params': {'sessionId': 's', 'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'hello'}}}}}
        row = normalize(raw)
        self.assertEqual(row['event_type'], 'assistant.output')
        self.assertFalse(row['payload']['content_complete'])
        raw['payload']['method'] = 'session/request_permission'
        self.assertEqual(normalize(raw)['event_type'], 'permission.request')
        self.assertFalse(assess([normalize(raw)], {}, '42:1.0')['control']['blocking_verified'])

    def test_acp_tool_updates_do_not_duplicate_before(self):
        def row(update):
            return normalize({'payload': {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 's', 'update': update}}})
        self.assertEqual(row({'sessionUpdate': 'tool_call', 'toolCallId': 'a', 'rawInput': {}})['event_type'], 'tool.execute.before')
        self.assertEqual(row({'sessionUpdate': 'tool_call_update', 'toolCallId': 'a', 'status': 'in_progress'})['event_type'], 'tool.progress')
        self.assertEqual(row({'sessionUpdate': 'tool_call_update', 'toolCallId': 'a', 'status': 'completed', 'rawOutput': ''})['event_type'], 'tool.execute.after')


class SelfcheckTests(unittest.TestCase):
    def fixture(self):
        iid = '42:1.0'
        rows = [{'instance_id': iid, 'event_type': 'hook.loaded', 'payload': {'event': 'hook.loaded'}}]
        for n, k in enumerate(KINDS, 1):
            rows.append({'instance_id': iid, 'event_type': k, 'payload': {'event': k, 'session_id': 's', 'turn_id': 't',
                'event_id': str(n), 'sequence': n, 'request_id': 'r', 'tool_call_id': 'c', 'content': {'text': '真实正文'}, 'content_complete': True}})
        rows.append({'instance_id': iid, 'event_type': 'io.turn.end', 'payload': {'event': 'io.turn.end',
            'session_id': 's', 'turn_id': 't', 'last_sequence': 6, 'expected_counts': dict.fromkeys(KINDS, 1)}})
        return [normalize(r) for r in rows]

    def test_acknowledgment_does_not_pass_control_and_old_proof_cannot_transfer(self):
        rows = self.fixture()
        proof = {'current': True, 'target': {'pid': 42, 'create_time': 1.0}, 'checks': ['allow_effect', 'deny_effect']}
        result = assess(rows, {}, '42:1.0', [proof])
        self.assertEqual(result['status'], 'pending')
        self.assertFalse(result['control']['blocking_verified'])
        proof['checks'].append('decision_wait')
        self.assertEqual(assess(rows, {}, '42:1.0', [proof])['status'], 'passed')
        proof['current'] = False
        self.assertFalse(assess(rows, {}, '42:1.0', [proof])['control']['effects_verified'])
        proof['current'] = True
        proof['target']['create_time'] = 2.0
        self.assertFalse(assess(rows, {}, '42:1.0', [proof])['control']['effects_verified'])

    def test_lost_response_and_no_events_never_pass(self):
        rows = [r for r in self.fixture() if r['event_type'] != 'model.response']
        result = assess(rows, {}, '42:1.0')
        self.assertIn('io_round', result['next_checks'])
        self.assertEqual(assess([], {}, '42:1.0')['passed'], 0)
        self.assertEqual(assess(self.fixture(), {'source': {'truncated': True}}, '42:1.0')['status'], 'failed')


if __name__ == '__main__': unittest.main()
