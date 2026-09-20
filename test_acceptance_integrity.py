import unittest
from e2e.acceptance_report import aggregate_status
from e2e.verify_control_confirm_batch import evaluate_results

class AcceptanceIntegrityTests(unittest.TestCase):
    def test_probe_cannot_cross_executable_or_directory(self):
        from runtime.capability import scoped_trust
        report = {'status':'probed','source':{'binary':'/a/runtime','cwd':'/a/home'}}
        self.assertEqual(scoped_trust(report, {'exe':'/b/runtime','cwd':'/a/home'})['status'], 'scope_unverified')
        self.assertEqual(scoped_trust(report, {'exe':'/a/runtime','cwd':'/b/home'})['status'], 'scope_unverified')
        self.assertEqual(scoped_trust(report, {'exe':'/a/runtime','cwd':'/a/home'}), report)

    def test_dashboard_does_not_apply_global_trust_to_other_runtime(self):
        from unittest.mock import Mock, patch
        import monitor_dashboard
        process = Mock()
        process.exe.return_value = '/different/runtime'
        process.cwd.return_value = '/same/project'
        snapshot = {'agents':[{'pid':123,'instance_id':'123:1.0','adapter':{}}]}
        trust = {'status':'probed','source':{'binary':'/probe/runtime','cwd':'/same/project'},
                 'owned':{'total':10,'trusted':10}}
        with patch.object(monitor_dashboard.psutil,'Process',return_value=process):
            monitor_dashboard.attach_capability(snapshot,trust=trust,serving={'status':'stopped'})
        stages = {r['id']:r for r in snapshot['agents'][0]['adapter']['capability']['stages']}
        self.assertEqual(stages['trusted']['state'],'unknown')

    def test_dashboard_card_surfaces_partial_io_from_live_window(self):
        import monitor_dashboard
        hint = monitor_dashboard._io_hint({'recent_events': [
            {'event_type': 'user.prompt.submitted'}, {'event_type': 'assistant.output'}
        ]})
        self.assertEqual(hint['status'], 'gaps')
        observed = {row['event'] for row in hint['capabilities'] if row['observed']}
        self.assertEqual(observed, {'user.input', 'assistant.output'})

    def test_failure_wins_over_external_condition(self):
        self.assertEqual(aggregate_status([{'status':'失败'}, {'status':'待外部条件'}]), '失败')

    def test_missing_local_test_is_not_external(self):
        self.assertEqual(aggregate_status([{'status':'未执行'}, {'status':'待外部条件'}]), '未执行')

    def test_runner_nonzero_exit_overrides_success_text(self):
        from unittest.mock import patch
        from subprocess import CompletedProcess
        from e2e.run_all_acceptance import run
        with patch('e2e.run_all_acceptance.subprocess.run', return_value=CompletedProcess([],1,'{"passed":true}', '')):
            self.assertFalse(run(['test.py'])['ok'])

    def test_runner_timeout_records_failure(self):
        from unittest.mock import patch
        from subprocess import TimeoutExpired
        from e2e.run_all_acceptance import run
        with patch('e2e.run_all_acceptance.subprocess.run', side_effect=TimeoutExpired('test',3600)):
            self.assertFalse(run(['test.py'])['ok'])

    def test_tool_output_echo_is_not_user_input(self):
        import json, tempfile
        from pathlib import Path
        from e2e.verify_normal_conversation import reconcile, transcript_baseline
        baseline = [ {'type':'turn_context','payload':{'turn_id':'t'}},
                     {'type':'response_item','payload':{'type':'message','role':'user','content':[{'text':'hello'}]}},
                     {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'text':'world'}]}} ]
        records = [{'event_type':'tool.execute.after','payload':{'turn_id':'t','prompt':'hello'}},
                   {'event_type':'assistant.output','payload':{'turn_id':'t','assistant_output':'world'}}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'session.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in baseline))
            result = reconcile(records,transcript_baseline([str(path)]))
        self.assertEqual(result['completed_turns_exact'],0)
        self.assertEqual(result['mismatches'],1)
        self.assertFalse(result['passed'])

    def test_empty_report_cannot_pass(self):
        self.assertEqual(aggregate_status([]), '未执行')
        self.assertFalse(any(evaluate_results({}).values()))

    def test_one_round_cannot_claim_five(self):
        report = {'approve':[{'paused_before_effect':True,'created_once':True,'errors':[]}]}
        self.assertFalse(evaluate_results(report)['approve_5_of_5'])

    def test_transport_error_is_not_rejected_decision(self):
        report = {'duplicate':[{'late_decision_codes':[-1,-1], 'created_once':True,'errors':[]}]*5}
        self.assertFalse(evaluate_results(report)['late_decision_rejected_10_of_10'])

    def test_real_rejections_require_ten_responses(self):
        report = {'duplicate':[{'late_decision_codes':[400,400], 'created_once':True,'errors':[]}]*5}
        self.assertTrue(evaluate_results(report)['late_decision_rejected_10_of_10'])

    def test_timeout_cannot_pass_as_clean_approval(self):
        report = {'approve':[{'paused_before_effect':True,'created_once':True,'errors':['timeout']}]*5}
        self.assertFalse(evaluate_results(report)['approve_5_of_5'])

    def _prefix_records(self, truncation):
        # 10 turns where the hook only captured a prefix of every message.
        records = []
        for i in range(10):
            payload = {'turn_id': 't%d' % i, 'timestamp': 1000 + i,
                       'redaction': {'credentials_exported': False}}
            if truncation is not None:
                payload['truncation'] = truncation
            records.append(dict(event_type='user.input',
                                payload={**payload, 'prompt': 'hello'}))
            records.append(dict(event_type='assistant.output',
                                payload={**payload, 'assistant_output': 'world'}))
        return records

    def _prefix_baseline(self, directory):
        import json
        from pathlib import Path
        from e2e.verify_normal_conversation import transcript_baseline
        rows = []
        for i in range(10):
            rows.append({'type': 'turn_context', 'payload': {'turn_id': 't%d' % i}})
            rows.append({'type': 'response_item', 'timestamp': '2026-01-01T00:00:00+00:00',
                         'payload': {'type': 'message', 'role': 'user',
                                     'content': [{'text': 'hello full message'}]}})
            rows.append({'type': 'response_item', 'timestamp': '2026-01-01T00:00:01+00:00',
                         'payload': {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                                     'content': [{'text': 'world full answer'}]}})
        path = Path(directory) / 'session.jsonl'
        path.write_text('\n'.join(json.dumps(r) for r in rows))
        return transcript_baseline([str(path)])

    def test_undeclared_prefix_is_never_verbatim(self):
        # A generic redaction envelope must not launder a truncated capture.
        import tempfile
        from e2e.verify_normal_conversation import reconcile
        with tempfile.TemporaryDirectory() as directory:
            result = reconcile(self._prefix_records(None), self._prefix_baseline(directory))
        self.assertEqual(result['completed_turns_exact'], 0)
        self.assertEqual(result['partial_declared_messages'], 0)
        self.assertGreater(result['mismatches'], 0)
        self.assertFalse(result['passed'])

    def test_declared_prefix_is_gap_not_verbatim(self):
        # Even an honestly declared truncation never counts as a verbatim turn.
        import tempfile
        from e2e.verify_normal_conversation import reconcile
        truncation = {'truncated': True, 'fields': [{'field': 'prompt'}]}
        with tempfile.TemporaryDirectory() as directory:
            result = reconcile(self._prefix_records(truncation), self._prefix_baseline(directory))
        self.assertEqual(result['completed_turns_exact'], 0)
        self.assertEqual(result['partial_declared_messages'], 20)
        self.assertFalse(result['passed'])

if __name__ == '__main__':
    unittest.main()
