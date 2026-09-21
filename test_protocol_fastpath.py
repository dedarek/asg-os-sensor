import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import psutil

from runtime import protocol_fastpath as fast, autonomous_pipeline as pipeline, learned_install
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

    def test_transaction_real_callback_and_rollback_without_goose(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace, state = root / 'workspace', root / 'state'
            workspace.mkdir(); state.mkdir()
            config = workspace / 'settings.json'
            original = json.dumps({'model': 'preserve', 'hooks': {
                'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'original-callback'}]}],
                'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'original-callback'}]}],
                'Stop': [{'hooks': [{'type': 'command', 'command': 'original-callback'}]}]}})
            config.write_text(original)
            target = {'pid': os.getpid(), 'create_time': psutil.Process().create_time()}
            with patch.dict(os.environ, {'ASG_RUN_DIR': str(state), 'ASG_ONBOARDING_AUTHORIZED': '1',
                    'ASG_ONBOARDING_AUTO_INSTALL': '1', 'ASG_ONBOARDING_SCOPE': 'project',
                    'ASG_ONBOARDING_WORKSPACE_ROOTS': str(workspace)}):
                result = fast.install(config, target, state, [])
                self.assertEqual(result['status'], 'installed')
                saved = json.loads(config.read_text())
                self.assertEqual(saved['model'], 'preserve')
                self.assertEqual(saved['hooks']['UserPromptSubmit'][0]['hooks'][0]['command'], 'original-callback')
                hook = workspace / '.asg-command-protocol' / 'hook.py'
                client = hook.with_name('client.json')
                # This is a real child invocation of the installed generic Hook,
                # not a mocked file write or a claimed real Agent acceptance.
                for payload in ({'hook_event_name': 'UserPromptSubmit', 'session_id': 's', 'prompt': '真实协议回调 输入'},
                                {'hook_event_name': 'Stop', 'session_id': 's', 'last_assistant_message': '真实协议回调 输出'}):
                    run = subprocess.run([sys.executable, str(hook), str(client)], input=json.dumps(payload),
                                         text=True, capture_output=True, timeout=10)
                    self.assertEqual(run.returncode, 0, run.stderr)
                from runtime.hook_data import snapshot
                data = snapshot(state, pid=target['pid'], create_time=target['create_time'])
                self.assertEqual([m['text'] for m in data['conversation']], ['真实协议回调 输入', '真实协议回调 输出'])
                checks = data['coverage']['integration_selfchecks'][0]
                self.assertGreaterEqual(checks['passed'], 3)
                self.assertNotEqual(checks['status'], 'passed')  # no model/control evidence
                learned_install.rollback(workspace, Path(result['state_dir']), approved_workspace=workspace,
                                         approved_digest=result['plan_digest'])
                self.assertEqual(config.read_text(), original)
                self.assertFalse(hook.exists())

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

    def test_fallback_is_once_per_instance_and_records_gaps(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_RUN_DIR': tmp}):
            target = {'pid': 123, 'create_time': 1.0}
            iid = '123:1.0'
            pipeline.save(iid, 'protocol_fastpath', '', status='installed', installed_at=0)
            with patch.object(pipeline, 'verify_installed', return_value={'valid_events': 2, 'selfcheck': {'status': 'pending', 'next_checks': ['io_round']}}):
                self.assertFalse(fast.attempt(target)['handled'])
                self.assertEqual(pipeline.read(iid)['upgrade']['missing_checks'], ['io_round'])
            with patch.object(pipeline, 'verify_installed', side_effect=AssertionError('must not retry install')):
                self.assertFalse(fast.attempt(target)['handled'])

    def test_pending_activation_does_not_call_goose_or_claim_success(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_RUN_DIR': tmp}):
            target = {'pid': 123, 'create_time': 1.0}
            pipeline.save('123:1.0', 'protocol_fastpath', '', status='installed', installed_at=time.time())
            with patch.object(pipeline, 'verify_installed', return_value={'valid_events': 0}):
                result = fast.attempt(target)
                self.assertTrue(result['handled'])
                self.assertFalse(result['verified'])


if __name__ == '__main__': unittest.main()
