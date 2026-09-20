import unittest
from unittest.mock import patch, Mock
from .discovery import confirmed

class DiscoveryTest(unittest.TestCase):
    def target(self,status='confirmed_agent',role='agent'):
        return {'pid':42,'instance_id':'42:123.5','name':'Example','identity':{'id':'unknown-runtime'},'adapter':{'agent_classification':{'status':status,'roles':[role]},'assets':{}}}
    def test_pending_and_confirmed_live_instances_without_infrastructure(self):
        process=Mock();process.create_time.return_value=123.5;process.cwd.return_value='/workspace';process.environ.return_value={'OPENCODE_CONFIG_DIR':'/config','API_KEY':'SECRET'}
        with patch('psutil.Process',return_value=process):
            items=list(confirmed({'agents':[self.target(),self.target('pending'),self.target(role='model_gateway')]}))
        self.assertEqual(len(items),2);self.assertEqual(items[1]["classification"],"pending");self.assertEqual(items[0]['collection_environment'],{'OPENCODE_CONFIG_DIR':'/config'})
        self.assertEqual(items[0]['asg_instance_id'],'42:123.5')
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
    def test_reused_pid_rejected(self):
        process=Mock();process.create_time.return_value=124
        with patch('psutil.Process',return_value=process):self.assertEqual(list(confirmed({'agents':[self.target()]})),[])

if __name__=='__main__':unittest.main()
