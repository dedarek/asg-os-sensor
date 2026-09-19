import tempfile
import unittest
from unittest.mock import patch,Mock
from .endpoint import Endpoint
class ManualCollectionTest(unittest.TestCase):
 def test_idle_manual_service_does_not_collect_or_send(self):
  with tempfile.TemporaryDirectory() as root:
   e=Endpoint({'state_dir':root,'backend_url':'http://127.0.0.1:1','collection_mode':'manual','agents':[]})
   e.flush=Mock();e.enqueue=Mock()
   with patch('integrations.soc_inventory.endpoint.time.sleep',side_effect=InterruptedError):
    with self.assertRaises(InterruptedError):e.run()
   e.flush.assert_not_called();e.enqueue.assert_not_called()
if __name__=='__main__':unittest.main()
