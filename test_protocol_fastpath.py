import json
import time
import unittest
from unittest.mock import patch

from runtime import protocol_fastpath as fast
from runtime.command_protocol_hook import handle
from runtime.integration_protocol import detect


class DirectProtocolTests(unittest.TestCase):
    def test_alias_dialect_detected_and_handled_structurally(self):
        # A host whose documented event spellings are the alias names must be
        # detected and answered with its own event name; recognition stays
        # structural and never keys on a product identity.
        found = detect({'hooks': {'BeforeTool': [{'type': 'command', 'command': 'echo'}],
                                  'AfterAgent': [{'type': 'command', 'command': 'echo'}]}})
        self.assertEqual([(c['family'], sorted(c['events'])) for c in found],
                         [('command_hooks', ['AfterAgent', 'BeforeTool'])])
        emitted = []
        decided = []
        def decide(request):
            decided.append(request)
            return {'decision': 'deny'}
        response, code = handle({'hook_event_name': 'BeforeTool', 'session_id': 's',
                                 'tool_name': 'write_file', 'tool_input': {'path': 'x'}},
                                {'control_client': ['fixture']}, {'pid': 1, 'create_time': 1.0},
                                decide=decide, emit=emitted.append)
        self.assertEqual(code, 2)
        self.assertEqual(response['hookSpecificOutput']['hookEventName'], 'BeforeTool')
        self.assertEqual(response['decision'], 'block')
        self.assertEqual(decided[0]['tool'], 'write_file')
        # Allow keeps the legacy shape; alias hosts read the same default allow.
        response, code = handle({'hook_event_name': 'AfterAgent', 'session_id': 's',
                                 'prompt': '输入', 'prompt_response': '完整输出'},
                                {}, {'pid': 1, 'create_time': 1.0}, emit=emitted.append)
        self.assertEqual((response, code), ({}, 0))
        # user.input alias is exercised in the same shape as Stop below.
        response, code = handle({'hook_event_name': 'BeforeAgent', 'session_id': 's',
                                 'prompt': '别名输入'}, {}, {'pid': 1, 'create_time': 1.0},
                                emit=emitted.append)
        self.assertEqual((response, code), ({}, 0))
        kinds = [e['event'] for e in emitted]
        self.assertIn('user.input', kinds)
        self.assertIn('assistant.output', kinds)
        assistant = [e for e in emitted if e['event'] == 'assistant.output']
        self.assertEqual(assistant[0]['content'], '完整输出')

    def test_dashboard_tries_protocol_before_resolving_goose(self):
        import monitor_dashboard as dashboard
        with patch.object(dashboard, 'ONBOARDING_AUTO_INSTALL_ENABLED', True), \
             patch.object(fast, 'attempt', return_value={'handled': True, 'reason': 'awaiting callback'}), \
             patch.object(dashboard, '_record_investigation_result'), \
             patch.object(dashboard, '_goose_executable', side_effect=AssertionError('Goose must not run')):
            dashboard._execute_investigation(123, {}, '123:1.0', 1.0)

    def test_dashboard_falls_back_to_goose_when_protocol_unavailable(self):
        import monitor_dashboard as dashboard
        with patch.object(dashboard, 'ONBOARDING_AUTO_INSTALL_ENABLED', True), \
             patch.object(fast, 'attempt', return_value={'handled': False, 'reason': 'no protocol'}), \
             patch.object(dashboard, '_record_investigation_result'), \
             patch.object(dashboard, '_record_onboarding_outcome'), \
             patch.object(dashboard, '_goose_executable', return_value=None) as goose:
            dashboard._execute_investigation(123, {}, '123:1.0', 1.0)
            goose.assert_called_once()

    def test_decision_is_waited_for_and_errors_deny(self):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'write', 'tool_input': {'path': 'canary'}, 'tool_use_id': 'c'}
        def delayed(request):
            time.sleep(.05)
            return {'decision': 'deny'}
        start = time.monotonic()
        response, code = handle(payload, {'control_client': ['fixture']}, {'pid': 1, 'create_time': 1.0}, decide=delayed, emit=lambda _: None)
        self.assertGreaterEqual(time.monotonic() - start, .05)
        self.assertEqual(code, 2)
        self.assertEqual(response['hookSpecificOutput']['permissionDecision'], 'deny')
        def broken(request): raise OSError('unavailable')
        self.assertEqual(handle(payload, {'control_client': ['fixture']}, {}, decide=broken, emit=lambda _: None)[1], 2)



if __name__ == '__main__': unittest.main()
