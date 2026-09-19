import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from runtime import local_assets
import monitor_dashboard as d

class RefreshTests(unittest.TestCase):
    def test_instance_change_discards_assets(self):
        proc=Mock();proc.create_time.return_value=999
        with patch('runtime.local_assets.psutil.Process',return_value=proc), patch('runtime.local_assets.collect') as collect:
            local_assets._refresh((12,123,None))
        collect.assert_not_called()
        self.assertEqual(local_assets._cache[(12,123,None)]['assets'],{})

    def test_completed_identity_refreshes_without_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp,'investigation_findings.json').write_text(json.dumps({'target':{'pid':12,'create_time':123},'findings':{'identity':{'status':'identified','value':{'roles':['model_gateway']},'evidence_refs':['ev-1']}}}))
            state={'agents':[{'pid':12,'instance_id':'12:123','adapter':{}}]}
            result={'12:123':{'status':'assets_collected','log_dir':tmp}}
            with patch.object(d,'SCAN_STATE',state),patch.object(d,'INVESTIGATION_RESULTS',result),patch.object(d,'refresh_hook_observations'),patch.object(d,'attach_capability'),patch.object(d,'_native_trust_view',return_value={}),patch.object(d,'_serving_view',return_value={}),patch('runtime.local_assets.current',return_value={'assets':{}}),patch('runtime.hook_approval.attach'):
                snapshot=d._enriched_snapshot()
            self.assertEqual(snapshot['agents'][0]['adapter']['agent_classification']['status'],'infrastructure')
            self.assertNotIn('agent_classification',state['agents'][0]['adapter'])
