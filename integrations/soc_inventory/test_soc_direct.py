import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile,json
from .package_runtime.soc_client import exchange
class DirectClientTests(unittest.TestCase):
    def test_event_is_sent_to_soc_with_bound_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            key=Path(tmp)/'key';key.write_text('unit-test-key')
            cfg={'backend_url':'http://127.0.0.1:8095','agent_id':'unit','platform':'example','instance_id':'12:34.5','token_file':str(key)}
            with patch('integrations.soc_inventory.package_runtime.soc_client.request',return_value={'accepted':True}) as send:
                result=exchange(cfg,{'event':'user.input','timestamp':'now','content':'hello'},'event')
                path,payload=send.call_args.args[1:]
                self.assertEqual(path,'/api/asg/events')
                self.assertEqual(payload['instance_id'],'12:34.5')
                self.assertEqual(payload['events'][0]['event_type'],'user.input')
                self.assertEqual(len(payload['events'][0]['event_id']),64)
                self.assertTrue(result['accepted'])

    def test_direct_wire_contract_and_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            key=Path(tmp)/'key';key.write_text('unit-test-key')
            cfg={'backend_url':'http://127.0.0.1:8095','agent_id':'unit','platform':'example','token_file':str(key)}
            for action,decision in [('allow','allow'),('block','deny'),('confirm','deny')]:
                with patch('integrations.soc_inventory.package_runtime.soc_client.build_opener') as factory:
                    factory.return_value.open.return_value.__enter__.return_value.read.return_value=json.dumps({'action':action}).encode()
                    result=exchange(cfg,{'pid':12,'tool':'write','call_id':'one','input':{'path':'test'}},'decision')
                    req=factory.return_value.open.call_args.args[0]
                    self.assertEqual(req.full_url,'http://127.0.0.1:8095/api/analyze/before-tool-call')
                    self.assertEqual(json.loads(req.data)['tool']['args'],{'path':'test'})
                    self.assertEqual(result['decision'],decision)
                    self.assertNotIn('create_time',json.loads(req.data))
            with patch('integrations.soc_inventory.package_runtime.soc_client.build_opener') as factory:
                factory.return_value.open.return_value.__enter__.return_value.read.return_value=b'{}'
                with self.assertRaises(ValueError):exchange(cfg,{},'decision')
