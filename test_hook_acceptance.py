import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import psutil
from runtime import hook_acceptance as a
class AcceptanceTests(unittest.TestCase):
    def test_byte_changes_invalidate_instance_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'ASG_RUN_DIR':tmp}):
            r=Path(tmp)/'report.json';r.write_text('{}');hook=Path(tmp)/'hook.py';hook.write_text('old')
            t={'pid':os.getpid(),'create_time':psutil.Process().create_time()}
            a.record(t,r,[hook],['allow_effect','deny_effect']);self.assertTrue(a.snapshots()[0]['current'])
            hook.write_text('changed');self.assertFalse(a.snapshots()[0]['current'])
