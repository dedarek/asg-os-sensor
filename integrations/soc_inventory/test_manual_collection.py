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
 def test_manual_request_consumes_then_proceeds(self):
  with tempfile.TemporaryDirectory() as root:
   e=Endpoint({'state_dir':root,'backend_url':'http://127.0.0.1:1','collection_mode':'manual','agents':[]})
   e.db.execute('INSERT INTO collection_requests DEFAULT VALUES')
   e.flush=Mock()
   with patch('integrations.soc_inventory.endpoint.time.sleep',side_effect=InterruptedError):
    with self.assertRaises(InterruptedError):e.run()
   self.assertEqual(e.db.execute('SELECT count(*) FROM collection_requests').fetchone()[0],0)
if __name__=='__main__':unittest.main()
