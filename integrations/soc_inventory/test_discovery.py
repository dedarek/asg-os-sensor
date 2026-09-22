import unittest
from unittest.mock import patch, Mock
from .discovery import confirmed,platform_id

class DiscoveryTest(unittest.TestCase):
    def test_package_identity_becomes_portable_platform_id(self):
        self.assertEqual(platform_id({'id':'package:/Users/a/.npm/node_modules/@deepseek-ai/dsh'}),'deepseek-ai-dsh')
        self.assertEqual(platform_id({'id':'OpenCode'}),'opencode')
    def target(self,status='confirmed_agent',role='agent',score=50):
        return {'pid':42,'instance_id':'42:123.5','name':'Example','score':score,'identity':{'id':'unknown-runtime'},'adapter':{'agent_classification':{'status':status,'roles':[role]},'assets':{}}}
    def test_pending_and_confirmed_live_instances_without_infrastructure(self):
        process=Mock();process.create_time.return_value=123.5;process.cwd.return_value='/workspace';process.environ.return_value={'OPENCODE_CONFIG_DIR':'/config','API_KEY':'SECRET'}
        with patch('psutil.Process',return_value=process):
            items=list(confirmed({'agents':[self.target(),self.target('pending'),self.target(role='model_gateway')]}))
        self.assertEqual(len(items),2);self.assertEqual(items[1]["classification"],"pending");self.assertEqual(items[0]['collection_environment'],{'OPENCODE_CONFIG_DIR':'/config'})
        self.assertEqual(items[0]['asg_instance_id'],'42:123.5')
        self.assertEqual(items[0]['asset_id'],items[1]['asset_id'])
    def test_pending_process_without_agent_signal_is_not_registered(self):
        process=Mock();process.create_time.return_value=123.5;process.cwd.return_value='/workspace';process.environ.return_value={}
        plain=self.target('pending',score=0)
        plain['identity']={'id':'package:/tmp/node_modules/vite','protocol_dependencies':[]}
        plain['discovery_evidence']={'signals':[{'source':'package-bin-mapping'},{'source':'model-transport-connection'}]}
        protocol=self.target('pending',score=0)
        protocol['identity']={'id':'package:/tmp/node_modules/example','protocol_dependencies':['example-acp']}
        with patch('psutil.Process',return_value=process):
            items=list(confirmed({'agents':[plain,protocol]}))
        self.assertEqual([item['name'] for item in items],['Example'])
        self.assertEqual(items[0]['platform'],'example')
    def test_new_offline_target_cached_and_replayed_after_exit(self):
        import tempfile,json
        from pathlib import Path
        from .endpoint import Endpoint
        from .discovery import Discovery
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);skill=root/'.hermes/skills/example';skill.mkdir(parents=True)
            (skill/'SKILL.md').write_text('---\nname: offline-original\ndescription: test\n---')
            cfg={'backend_url':'http://127.0.0.1:1','state_dir':directory,'agents':[], 'discovery':{'application_key_file':'unused'}}
            e=Endpoint(cfg);discovery=Discovery(e)
            target={'name':'Example','asg_instance_id':'42:123.5','platform':'hermes','workspace':directory,'collection_environment':{'HERMES_HOME':str(root/'.hermes')}}
            e.request=lambda *args: (_ for _ in ()).throw(OSError('offline'))
            with patch('integrations.soc_inventory.discovery.build_opener') as opener, patch('integrations.soc_inventory.discovery.confirmed',return_value=iter([target])):
                opener.return_value.open.side_effect=OSError('ASG unavailable')
                self.assertEqual(discovery.refresh(),[])
            self.assertEqual(e.db.execute('SELECT count(*) FROM pending_discoveries').fetchone()[0],1);e.db.close()
            (skill/'SKILL.md').write_text('changed')
            resumed=Endpoint(cfg);discovery=Discovery(resumed);resumed.request=lambda *args:{'agent_id':'enrolled','api_key':'test-key'}
            with patch('integrations.soc_inventory.discovery.build_opener') as opener:
                opener.return_value.open.side_effect=OSError('ASG unavailable')
                self.assertEqual(discovery.refresh(),['enrolled'])
            draft=json.loads(resumed.db.execute('SELECT body FROM drafts').fetchone()[0])
            self.assertEqual(draft['categories']['skill']['scopes'][0]['items'][0]['name'],'offline-original')
            self.assertEqual(draft['agent_id'],'enrolled')
            self.assertEqual(resumed.db.execute('SELECT count(*) FROM pending_discoveries').fetchone()[0],0)
    def test_create_time_flap_transfers_enrollment_instead_of_dual_enroll(self):
        # macOS create_time can flap by ~1s for one live process. The collector
        # must move the enrollment and onboarding receipt to the new stamp, not
        # register the same process a second time under a duplicate SOC card.
        import tempfile, json
        from .endpoint import Endpoint
        from .discovery import Discovery
        with tempfile.TemporaryDirectory() as directory:
            cfg={'backend_url':'http://127.0.0.1:1','state_dir':directory,'agents':[], 'discovery':{'application_key_file':'unused'}}
            e=Endpoint(cfg);discovery=Discovery(e)
            prior={'name':'Example','platform':'codex','workspace':'/','asg_instance_id':'42:100.0','source_instance_id':'42:99.0','identity_refreshed':True,'agent_id':'asg-old','key_file':'unused','classification':'pending'}
            e.db.execute('INSERT INTO enrolled VALUES(?,?)',('42:100.0',json.dumps(prior)))
            e.db.execute('INSERT INTO soc_onboarding VALUES(?,?)',('42:100.0',json.dumps({'status':'installed','artifact_id':'a1'})))
            drifted={'name':'Example','asg_instance_id':'42:101.0','source_instance_id':'42:100.0','identity_refreshed':True,'platform':'codex','workspace':'/','collection_environment':{}}
            e.request=lambda *args: (_ for _ in ()).throw(AssertionError('must not re-enroll'))
            with patch('integrations.soc_inventory.discovery.confirmed',return_value=iter([drifted])):
                discovery.refresh()
            rows=e.db.execute('SELECT instance,configuration FROM enrolled').fetchall()
            self.assertEqual([row[0] for row in rows],['42:101.0'])
            self.assertEqual(json.loads(rows[0][1])['agent_id'],'asg-old')
            onboard=e.db.execute('SELECT instance,result FROM soc_onboarding').fetchall()
            self.assertEqual([row[0] for row in onboard],['42:101.0'])
            self.assertEqual(e.db.execute('SELECT count(*) FROM pending_discoveries').fetchone()[0],0)
            e.db.close()
    def test_legacy_instance_enrollment_migrates_to_durable_asset(self):
        import tempfile,json
        from .endpoint import Endpoint
        from .discovery import Discovery,durable_asset_id
        with tempfile.TemporaryDirectory() as directory:
            cfg={'backend_url':'http://127.0.0.1:1','state_dir':directory,'agents':[],
                 'discovery':{'application_key_file':'unused'}}
            endpoint=Endpoint(cfg);discovery=Discovery(endpoint)
            current={'name':'Example','platform':'codex','workspace':'/',
                     'executable':'/Applications/Codex.app/Contents/MacOS/Codex',
                     'asg_instance_id':'42:123.5','collection_environment':{}}
            legacy={**current,'agent_id':'asg-instance-card','key_file':'unused'}
            endpoint.db.execute('INSERT INTO enrolled VALUES(?,?)',
                                (current['asg_instance_id'],json.dumps(legacy)))
            sent=[]
            endpoint.request=lambda _agent,_path,body:(sent.append(json.loads(body)),
                {'agent_id':'asg-asset-card','api_key':'test-key',
                 'asset_id':json.loads(body)['asset_id']})[1]
            with patch('integrations.soc_inventory.discovery.build_opener') as opener, \
                 patch('integrations.soc_inventory.discovery.confirmed',return_value=iter([current])):
                opener.return_value.open.side_effect=OSError('ASG unavailable')
                self.assertEqual(discovery.refresh(),['asg-asset-card'])
            self.assertEqual(sent[0]['asset_id'],durable_asset_id(current))
            saved=json.loads(endpoint.db.execute(
                'SELECT configuration FROM enrolled WHERE instance=?',
                (current['asg_instance_id'],)).fetchone()[0])
            self.assertEqual(saved['agent_id'],'asg-asset-card')
            self.assertEqual(saved['asset_id'],durable_asset_id(current))
            self.assertEqual(saved['enrollment_scope'],'asset')
            endpoint.db.close()
    def test_reused_pid_rejected(self):
        process=Mock();process.create_time.return_value=124
        with patch('psutil.Process',return_value=process):self.assertEqual(list(confirmed({'agents':[self.target()]})),[])

    def test_macos_create_time_flap_preserves_sensor_instance(self):
        target=self.target();target['identity']['evidence']='/Applications/Example.app/example'
        process=Mock();process.create_time.return_value=124.5
        process.exe.return_value='/Applications/Example.app/example'
        process.cmdline.return_value=['/Applications/Example.app/example']
        process.cwd.return_value='/workspace';process.environ.return_value={}
        with patch('psutil.Process',return_value=process):items=list(confirmed({'agents':[target]}))
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['asg_instance_id'],'42:123.5')
        self.assertFalse(items[0]['identity_refreshed'])

    def test_waiting_activation_is_reverified_without_upgrade(self):
        import tempfile,json
        from pathlib import Path
        from .endpoint import Endpoint
        from .discovery import Discovery
        with tempfile.TemporaryDirectory() as directory:
            cfg={'backend_url':'http://127.0.0.1:1','state_dir':directory,
                 'agents':[],'soc_installation':True,
                 'discovery':{'application_key_file':'unused'}}
            endpoint=Endpoint(cfg);discovery=Discovery(endpoint)
            key=Path(directory)/'key';key.write_text('test-key')
            agent={'name':'Example','asg_instance_id':'42:123.5',
                   'platform':'example','workspace':directory,
                   'agent_id':'asg-example','key_file':str(key)}
            endpoint.agents[agent['agent_id']]=agent
            endpoint.db.execute('INSERT INTO enrolled VALUES(?,?)',
                                (agent['asg_instance_id'],json.dumps(agent)))
            prior={'status':'installed_waiting_activation','artifact_id':'a1',
                   'checksum':'c1','transport':'soc-direct-v1',
                   'reason':'obsolete_failure','investigation_requested':True}
            endpoint.db.execute('INSERT INTO soc_onboarding VALUES(?,?)',
                                (agent['asg_instance_id'],json.dumps(prior)))
            process=Mock();process.create_time.return_value=123.5
            process.exe.return_value='/bin/sh'
            choice={'id':'a1','checksum':'c1','transport':'soc-direct-v1'}
            with patch('integrations.soc_inventory.discovery.confirmed',return_value=iter([agent])), \
                 patch('psutil.Process',return_value=process), \
                 patch('integrations.soc_inventory.soc_onboarding.select',return_value=choice), \
                 patch('integrations.soc_inventory.soc_onboarding.integrity',return_value=True), \
                 patch('integrations.soc_inventory.soc_onboarding.install',return_value={
                     'status':'activation_verified','artifact_id':'a1','checksum':'c1',
                     'transport':'soc-direct-v1'}) as install:
                discovery.refresh()
            install.assert_called_once_with(endpoint,agent,'/bin/sh',execute=True,
                                            selected=choice)
            saved=json.loads(endpoint.db.execute(
                'SELECT result FROM soc_onboarding WHERE instance=?',
                (agent['asg_instance_id'],)).fetchone()[0])
            self.assertEqual(saved['status'],'activation_verified')
            self.assertNotIn('reason',saved)
            self.assertNotIn('investigation_requested',saved)
            endpoint.db.close()

if __name__=='__main__':unittest.main()
