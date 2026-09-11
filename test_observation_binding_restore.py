import json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import psutil
import monitor_dashboard as m

class BindingRestoreTests(unittest.TestCase):
    def test_executor_binding_restores_after_globals_reset(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_RUN_DIR':tmp}), patch.object(m,'OBSERVE_CONFIG',''), patch.object(m,'OBSERVE_URL',''), patch.object(m,'OBSERVE_CONFIG_ERROR',''):
            target={'pid':os.getpid(),'create_time':psutil.Process().create_time()}
            path=Path(tmp)/'source.json'
            path.write_text(json.dumps({'target':target,'log_path':str(Path(tmp)/'events.jsonl')}))
            self.assertTrue(m._wire_observe_config({'status':'installed','config_path':str(path)},target))
            m.OBSERVE_CONFIG=''
            m._restore_observe_config()
            self.assertEqual(m.OBSERVE_CONFIG,str(path.resolve()))
            with self.assertRaises(ValueError):
                m._wire_observe_config({'status':'bound','config_path':str(path)},{**target,'create_time':1.0})
    def test_failed_install_never_publishes(self):
        self.assertFalse(m._wire_observe_config({'status':'failed','config_path':'missing'},{}))
