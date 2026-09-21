import tempfile
import unittest
from unittest.mock import patch,Mock
from .endpoint import Endpoint
class ManualCollectionTest(unittest.TestCase):
 def test_idle_manual_service_does_not_deep_collect(self):
  # Manual idle mode must never run deep asset collection (enqueue), but
  # durable outbox replay stays scheduled on its own so cached reports from
  # before/without a manual click still reach SOC.
  with tempfile.TemporaryDirectory() as root:
   e=Endpoint({'state_dir':root,'backend_url':'http://127.0.0.1:1','collection_mode':'manual','agents':[]})
   e.flush=Mock();e.enqueue=Mock()
   with patch('integrations.soc_inventory.endpoint.time.sleep',side_effect=InterruptedError):
    with self.assertRaises(InterruptedError):e.run()
   e.enqueue.assert_not_called()
   self.assertTrue(e.flush.called)
 def test_idle_manual_service_still_runs_lifecycle_discovery(self):
  # New instances must keep being discovered/registered in manual mode; only
  # the deep per-agent collection loop is gated.
  with tempfile.TemporaryDirectory() as root:
   e=Endpoint({'state_dir':root,'backend_url':'http://127.0.0.1:1','collection_mode':'manual','agents':[],'discovery':{'application_key_file':'unused'}})
   class D:
    def __init__(self,endpoint):self.refresh=Mock(return_value=[])
   e.flush=Mock();e.enqueue=Mock()
   import integrations.soc_inventory.discovery as discovery_module
   with patch.object(discovery_module,'Discovery',D),patch('integrations.soc_inventory.endpoint.time.sleep',side_effect=InterruptedError):
    with self.assertRaises(InterruptedError):e.run()
   e.enqueue.assert_not_called()
 def test_manual_mode_replays_cached_first_snapshot_without_deep_rescan(self):
  import json
  from pathlib import Path
  with tempfile.TemporaryDirectory() as root:
   key=Path(root)/'agent.key';key.write_text('test')
   agent={'agent_id':'a1','platform':'example','workspace':root,'key_file':str(key)}
   e=Endpoint({'state_dir':root,'backend_url':'http://127.0.0.1:1','collection_mode':'manual','agents':[agent]})
   identity,_=e.stream_identity(agent)
   cached={'agent_id':'a1','snapshot_id':'s1','categories':{
    'skill':{'status':'complete','scopes':[]},'mcp':{'status':'complete','scopes':[]}}}
   e.db.execute('INSERT INTO drafts VALUES(?,?)',(identity,json.dumps(cached).encode()))
   e.stream=Mock(return_value=(identity,('epoch',0)))
   e.request=Mock(return_value={'accepted':True})
   with patch('integrations.soc_inventory.endpoint.time.sleep',side_effect=InterruptedError):
    with self.assertRaises(InterruptedError):e.run()
   self.assertEqual(e.db.execute('SELECT count(*) FROM drafts').fetchone()[0],0)
   self.assertGreaterEqual(e.request.call_count,2)
 def test_manual_request_consumes_then_proceeds(self):
  with tempfile.TemporaryDirectory() as root:
   e=Endpoint({'state_dir':root,'backend_url':'http://127.0.0.1:1','collection_mode':'manual','agents':[]})
   e.db.execute('INSERT INTO collection_requests DEFAULT VALUES')
   e.flush=Mock()
   with patch('integrations.soc_inventory.endpoint.time.sleep',side_effect=InterruptedError):
    with self.assertRaises(InterruptedError):e.run()
   self.assertEqual(e.db.execute('SELECT count(*) FROM collection_requests').fetchone()[0],0)
if __name__=='__main__':unittest.main()
