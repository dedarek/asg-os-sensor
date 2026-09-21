"""Regression gates for channel transparency, oversized-report trimming,
per-agent failure isolation, and native package constraint pinning."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .event_spool import EventSpool
from .package_runtime.soc_client import exchange
from .runtime_bridge import RuntimeBridge
from .soc_onboarding import compatible


class ChannelTests(unittest.TestCase):
    def test_direct_transport_stamps_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp)/'key'; key.write_text('k')
            cfg = {'backend_url':'http://127.0.0.1:8095','agent_id':'unit','platform':'example',
                   'instance_id':'12:34.5','token_file':str(key)}
            with patch('integrations.soc_inventory.package_runtime.soc_client.request',
                       return_value={'accepted':True}) as send:
                exchange(cfg,{'event':'user.input','timestamp':'now','content':'x'},'event')
                payload = send.call_args.args[2]
                self.assertEqual(payload['events'][0]['channel'],'direct')

    def test_bridge_spool_stamps_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(str(Path(tmp)/'s.db'))
            endpoint = SimpleNamespace(db=db, request=lambda *a: {'accepted':True})
            agent = {'agent_id':'a','asg_instance_id':'42:100'}
            path = Path(tmp)/'events.jsonl'
            good = {'pid':42,'timestamp':101,'event':'user.input'}
            path.write_text(json.dumps(good)+'\n')
            binding = {'instance_id':'42:100','target':{'pid':42,'create_time':100},
                       'config':{'log_path':str(path)}}
            spool = EventSpool(endpoint)
            with patch('runtime.hook_data._registry_bindings',return_value=([binding],[],None)):
                spool.collect(agent,{'run_dir':tmp})
            body = db.execute('SELECT body FROM event_outbox').fetchone()[0]
            self.assertEqual(json.loads(body)['channel'],'bridge')


class BudgetTests(unittest.TestCase):
    def _bridge(self, tmp):
        db = sqlite3.connect(str(Path(tmp)/'s.db'))
        sent = []
        def request(agent,path,data,method='POST'):
            sent.append((path,json.loads(data) if data else None))
            return {'commands':[],'accepted':True}
        endpoint = SimpleNamespace(db=db, agents={}, config={'state_dir':tmp}, request=request)
        bridge = RuntimeBridge.__new__(RuntimeBridge)
        bridge.endpoint = endpoint
        bridge.base = 'http://127.0.0.1:8081'
        bridge.REPORT_BUDGET = 64*1024
        bridge.events = SimpleNamespace(collect=lambda *a: None, flush=lambda *a: None)
        return bridge, sent

    def test_oversized_hook_view_is_trimmed_and_labelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, sent = self._bridge(tmp)
            big = 'x'*(40*1024)
            hooks = {'records':[{'line':i,'timestamp':1,'event_type':'user.input',
                                 'payload':{'content':big}} for i in range(10)],
                     'coverage':{'limitations':[]}}
            bridge.local = lambda path, body=None: hooks
            agent = {'agent_id':'a','asg_instance_id':'1:9','name':'n'}
            state = {'agents':[{'instance_id':'1:9','pid':1}],'scan_interval':None,'native_trust':None}
            bridge._runtime_cycle(agent, state, {}, {})
            runtime = [p for p in sent if p[0]=='/api/asg/runtime']
            self.assertEqual(len(runtime),1)
            payload = runtime[0][1]['payload']
            kept = payload['hook_data']['records']
            self.assertLess(len(kept),10)
            self.assertEqual(payload['hook_data']['records_trimmed'],10-len(kept))
            self.assertTrue(any('网关上限' in t for t in payload['hook_data']['coverage']['limitations']))

    def test_one_failing_agent_does_not_strand_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, sent = self._bridge(tmp)
            seen = []
            def cycle(agent, state, controls, model, fingerprints=None):
                seen.append(agent['agent_id'])
                if agent['agent_id']=='bad': raise OSError('boom')
            bridge._runtime_cycle = cycle
            bridge.endpoint.agents = {'bad':{'agent_id':'bad'},'good':{'agent_id':'good'}}
            bridge.local = lambda path, body=None: {'agents':[],'scan_interval':None}
            bridge.run_once()
            self.assertEqual(seen, ['bad','good'])


class NativeConstraintTests(unittest.TestCase):
    def test_native_package_requires_declared_compatibility_scope(self):
        import platform as pf
        undeclared = {'adapter':'soc-native-v1','agent_type':'codex'}
        # Fail closed: without an auditable manifest no automatic install happens.
        self.assertFalse(compatible(undeclared, Path('/bin/ls'), 'codex'))
        declared = {'adapter':'soc-native-v1','agent_type':'codex','compatibility_declared':True,
                    'platforms':[pf.system()],'architectures':[pf.machine()],
                    'protocol_versions':['codex-hooks-v1']}
        self.assertTrue(compatible(declared, Path('/bin/ls'), 'codex'))
        self.assertFalse(compatible(declared, Path('/bin/ls'), 'opencode'))
        wrong_platform = dict(declared, platforms=['NonExistingOS'])
        self.assertFalse(compatible(wrong_platform, Path('/bin/ls'), 'codex'))
        # A manifest that claims a protocol this collector cannot build is a
        # mismatch: it must go to investigation, never to blind installation.
        foreign = dict(declared, protocol_versions=['some-future-hook-protocol'])
        self.assertFalse(compatible(foreign, Path('/bin/ls'), 'codex'))
        unknown_scope = dict(declared, platforms=[])
        self.assertFalse(compatible(unknown_scope, Path('/bin/ls'), 'codex'))

    def test_pinned_build_and_version_must_match(self):
        import hashlib
        import platform as pf
        from pathlib import Path
        exe = Path('/bin/ls')
        good = hashlib.sha256(exe.read_bytes()).hexdigest()
        base = {'adapter':'soc-native-v1','agent_type':'codex','compatibility_declared':True,
                'platforms':[pf.system()],'architectures':[pf.machine()],
                'protocol_versions':['codex-hooks-v1']}
        # An explicit executable pin is honoured: right build passes, wrong
        # build and unreadable executables are rejected, never a silent match.
        self.assertTrue(compatible({**base,'executable':good}, exe, 'codex'))
        self.assertFalse(compatible({**base,'executable':'0'*64}, exe, 'codex'))
        self.assertFalse(compatible({**base,'executable':good}, Path('/nonexistent/agent-bin'), 'codex'))
        # Version pins are release scopes; a wrong or missing version fails.
        self.assertTrue(compatible({**base,'version':'1.2.3'}, exe, 'codex', '1.2.3'))
        self.assertFalse(compatible({**base,'version':'1.2.3'}, exe, 'codex', '1.2.4'))
        self.assertFalse(compatible({**base,'version':'1.2.3'}, exe, 'codex', None))
        self.assertFalse(compatible({**base,'min_version':'2.0'}, exe, 'codex', '1.9.0'))
        self.assertTrue(compatible({**base,'min_version':'2.0'}, exe, 'codex', '2.0.1'))
        # Pins nested under compatibility for learned packages honour the
        # same rule (previously only the executable digest was checked there).
        learned = {'compatibility':{'platform':__import__('platform').system(),
                   'architecture':__import__('platform').machine(),'runtime':'native',
                   'version':'9.9.9'}}
        self.assertFalse(compatible(learned, exe, 'codex', '1.0.0'))

    def test_interpreter_package_requires_observed_entry_digest(self):
        import hashlib
        import platform as pf
        import sys
        exe=Path(sys.executable).resolve()
        entry=Path(__file__).resolve()
        stored={'platform':pf.system(),'architecture':pf.machine(),'runtime':'python',
                'executable':hashlib.sha256(exe.read_bytes()).hexdigest(),
                'entry':hashlib.sha256(entry.read_bytes()).hexdigest(),
                # Paths and cwd are intentionally portable metadata, not pins.
                'entry_path':'/another/machine/app.py','launch':'historical'}
        observed={**stored,'entry_path':str(entry),'launch':'current'}
        self.assertTrue(compatible({'compatibility':stored},exe,'unknown',
                                   observed_compatibility=observed))
        self.assertFalse(compatible({'compatibility':stored},exe,'unknown',
                                    observed_compatibility={**observed,'entry':'0'*64}))
        self.assertFalse(compatible({'compatibility':stored},exe,'unknown'))


if __name__=='__main__':
    unittest.main()
