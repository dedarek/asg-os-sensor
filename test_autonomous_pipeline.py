import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from runtime import autonomous_pipeline as pipeline
from runtime.observation_registry import Registry
from runtime import onboarding

class PipelineTests(unittest.TestCase):
    def test_restart_and_exact_reuse_keep_instance_assets_separate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_RUN_DIR': tmp}):
            self.assertIsNone(pipeline.next_phase('11:1', exact=True))
            pipeline.save('11:1', 'assets', '/run/a')
            self.assertEqual(pipeline.next_phase('11:1'), 'hook')
            self.assertIsNone(pipeline.next_phase('11:1', exact=True))
            pipeline.save('11:1', 'hook', '/run/b')
            self.assertEqual(pipeline.next_phase('11:1'), 'hook')
            self.assertIsNone(pipeline.next_phase('11:2', exact=True))
            self.assertTrue(json.loads(pipeline.path().read_text())['11:1']['hook'])

    def test_candidate_and_failed_reuse_do_not_complete_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_RUN_DIR': tmp}), \
             patch.object(onboarding, 'instance_state') as prior:
            pipeline.save('55:1', 'assets', '/run/a')
            pipeline.save('55:1', 'hook', '/run/b')
            for status in ('failed', 'reuse_requires_validation', 'unsupported'):
                prior.return_value = {'install': {'status': status}}
                self.assertEqual(pipeline.next_phase('55:1', exact=True), 'hook')
            prior.return_value = {'install': {'status': 'bound'}}
            self.assertIsNone(pipeline.next_phase('55:1', exact=True))
            pipeline.save('55:1', 'verification', '', status='verification_failed')
            self.assertEqual(pipeline.next_phase('55:1', exact=True), 'hook')
            pipeline.save('55:1','repair','',attempts=2,at=0)
            self.assertIsNone(pipeline.next_phase('55:1', exact=True))

    def test_file_plan_requires_live_evidence_for_learned_scope(self):
        # Arbitrary names/paths: no target-product branch is involved.  The
        # historical scope is accepted only when the live process is actually
        # rooted there; otherwise an old instance cannot redirect installation.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'arbitrary-agent' / 'config'
            cwd = workspace / 'session'
            cwd.mkdir(parents=True)
            recipe = {'hook': {'method': 'file_plan', 'workspace': str(workspace)}}
            plan = onboarding._common_plan({'pid': 55, 'create_time': 1., 'cwd': str(cwd)},
                                           'miss', None, recipe, 'goose')
            self.assertEqual(plan['workspace'], str(workspace))
            plan = onboarding._common_plan({'pid': 55, 'create_time': 1., 'cwd': '/'},
                                           'miss', None, recipe, 'goose')
            self.assertIsNone(plan['workspace'])
            recipe['hook'].pop('workspace')
            self.assertIsNone(onboarding._common_plan({'pid': 55, 'create_time': 1., 'cwd': '/tmp'},
                              'miss', None, recipe, 'goose')['workspace'])

    def test_infrastructure_does_not_install_agent_hook(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_RUN_DIR': tmp}):
            pipeline.save('1:1', 'assets', '/run/a', infrastructure=True)
            self.assertIsNone(pipeline.next_phase('1:1'))

    def test_installs_cannot_escape_deployment_workspace_scope(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'ASG_ONBOARDING_WORKSPACE_ROOTS': tmp + '/allowed',
                'ASG_ONBOARDING_AUTHORIZED': '1', 'ASG_ONBOARDING_AUTO_INSTALL': '1'}):
            self.assertFalse(onboarding.authorization_from_environment(tmp)['approved'])
            self.assertFalse(onboarding.authorization_from_environment(tmp + '/allowed-other')['approved'])
            self.assertTrue(onboarding.authorization_from_environment(tmp + '/allowed/project')['approved'])

class RegistryTests(unittest.TestCase):
    def test_two_live_bindings_and_historical_rebind_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            registry = Registry(root)
            original = root / 'config.json'
            def write(pid, ct):
                original.write_text(json.dumps({'target': {'pid': pid, 'create_time': ct},
                    'log_path': str(root / 'events.jsonl')}))
                return registry.register(original)
            first = write(101, 1.0)
            second = write(202, 2.0)
            # The second register changed the coordinator file, but not history.
            with patch('runtime.observation_source.snapshot', side_effect=lambda c: c['target']):
                snapshots = Registry(root).snapshots()
            self.assertEqual(snapshots[first], {'pid':101,'create_time':1.0})
            self.assertEqual(snapshots[second], {'pid':202,'create_time':2.0})
            with self.assertRaises(ValueError):
                registry.register(original, {'pid': 101, 'create_time': 1.0})

    def test_broken_binding_does_not_hide_other_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            registry = Registry(root)
            config = root / 'config.json'
            config.write_text(json.dumps({'target':{'pid':42,'create_time':1.0}, 'log_path':str(root/'events')}))
            iid = registry.register(config)
            Path(json.loads(registry.path.read_text())[iid]['config_path']).write_text('broken')
            self.assertEqual(registry.snapshots()[iid]['status'], 'unavailable')

class PhaseIntegrationTests(unittest.TestCase):
    def test_actual_dispatch_advances_assets_to_hook_without_manual_force(self):
        """Offline subprocess fixture, not a real-model acceptance claim."""
        import sys
        import psutil
        import monitor_dashboard as dashboard
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fake = root/'goose-fixture'
            fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
run = pathlib.Path(os.environ['ASG_AUDIT_DIR'])
(run/'arguments.json').write_text(json.dumps(sys.argv))
if os.environ['ASG_INVESTIGATION_PHASE'] == 'assets':
    target = {'pid':int(os.environ['ASG_TARGET_PID']), 'create_time':float(os.environ['ASG_TARGET_CREATE_TIME'])}
    (run/'evidence').mkdir(exist_ok=True)
    ref = 'ev-1-0123456789'
    (run/'evidence'/(ref+'.json')).write_text(json.dumps({'target':target,'evidence_id':ref}))
    finding = {'kind':'identity','status':'identified','value':{'name':'fixture','roles':['agent']},'evidence_refs':[ref]}
    (run/'investigation_findings.json').write_text(json.dumps({'version':1,'target':target,'findings':{'identity':finding,'assets':{}}}))
    sys.exit(0)
sys.exit(7)
''')
            fake.chmod(0o700)
            target = {'pid':os.getpid(), 'create_time':psutil.Process().create_time()}
            iid = '%s:%s' % (target['pid'],target['create_time'])
            route = {'provider':'openai','model':'fixture','route':'fixture'}
            env = {'ASG_PIPELINE':'1','ASG_RUN_DIR':str(root/'runs'),
                   'ASG_EXPERIENCE_DB':str(root/'exp.json'),'ASG_FINGERPRINT_DB':str(root/'fp.json')}
            try:
                with patch.dict(os.environ,env), patch.object(dashboard,'GOOSE',fake), \
                     patch.object(dashboard,'AUTONOMOUS_ANALYSIS_ENABLED',True), \
                     patch.object(dashboard,'analyst_route',return_value=route), \
                     patch.object(dashboard,'load_analyst_key',return_value='fixture'), \
                     patch.object(dashboard,'build_goose_env',return_value={}), \
                     patch.object(dashboard.matcher,'classify',return_value={'status':'miss'}):
                    dashboard.run_autonomous_investigation(target['pid'], target)
                    self.assertEqual(pipeline.next_phase(iid),'hook')
                    self.assertEqual(dashboard._enqueue_investigation(target['pid'],iid,target['create_time']),'enqueued')
                    with dashboard.INVESTIGATION_LOCK:
                        dashboard.INVESTIGATION_QUEUE.clear()
                        dashboard.INVESTIGATION_QUEUED.pop(iid,None)
                    dashboard.run_autonomous_investigation(target['pid'],target)
                    runs=sorted((root/'runs').glob('pid_*/arguments.json'))
                    self.assertEqual(len(runs),2)
                    recipes=[json.loads(p.read_text())[json.loads(p.read_text()).index('--recipe')+1] for p in runs]
                    self.assertTrue(recipes[0].endswith('runtime_assets.yaml'))
                    self.assertTrue(recipes[1].endswith('runtime_hook_analyst.yaml'))
                    self.assertEqual(pipeline.next_phase(iid),'hook')  # failed Hook is not completion
            finally:
                with dashboard.INVESTIGATION_LOCK:
                    dashboard.INVESTIGATION_RESULTS.pop(iid,None)
                    dashboard.INVESTIGATION_RETRY_AT.pop(iid,None)
                    dashboard.INVESTIGATING_INSTANCES.pop(iid,None)
