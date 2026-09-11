import json, tempfile, unittest
from pathlib import Path
from unittest.mock import Mock
from runtime.analyst_evidence import entry_surface

class ConfigDirectoryEvidenceTests(unittest.TestCase):
    def test_generic_declared_directory_not_secret_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); cfg=root/'unfamiliar-config';cfg.mkdir()
            p=Mock();p.pid=123;p.exe.return_value='';p.cmdline.return_value=[];p.cwd.return_value=str(root);p.open_files.return_value=[]
            p.parent.return_value=None;p.children.return_value=[];p.create_time.return_value=1.0
            p.environ.return_value={'ACME_CONFIG_DIR':str(cfg),'API_TOKEN':'never-export-this','SECRET_CONFIG_DIR':str(root/'private'),'HOME':str(root)}
            s=entry_surface(p);self.assertTrue(any(x['path']==str(cfg) and 'ACME_CONFIG_DIR' in x['source'] for x in s['related_roots']))
            self.assertNotIn('never-export-this',str(s))
