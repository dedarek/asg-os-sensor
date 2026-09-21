"""Protocol clues must not silently install a local-only runtime."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import psutil
from runtime import protocol_fastpath as fast, autonomous_pipeline as pipeline

class ProtocolAdmissionTests(unittest.TestCase):
    def test_protocol_discovery_preserves_loader_evidence(self):
        from runtime.protocol_discovery import inspect_paths
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'settings.json'
            config.write_text(json.dumps({'hooks': {'PreToolUse': [
                {'type': 'command', 'command': 'existing'}]}}))
            opened = inspect_paths([(config, 'opened_file')])['candidates'][0]
            nearby = inspect_paths([(config, 'related_root_convention')])['candidates'][0]
            self.assertTrue(opened['loaded_by_process'])
            self.assertFalse(nearby['loaded_by_process'])

    def test_document_keywords_and_single_config_never_authorize_install(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_RUN_DIR': tmp}):
            root = Path(tmp)
            config = root / 'settings.json'
            config.write_text('{"theme":"dark"}')
            (root / 'README.md').write_text('"BeforeTool" "AfterTool" {"type":"command"}')
            before = config.read_bytes()
            target = {'pid': os.getpid(), 'create_time': psutil.Process().create_time()}
            with patch('runtime.protocol_discovery.discover', return_value={
                    'candidates': [], 'checked_paths': [str(config)]}):
                result = fast.attempt(target)
            self.assertFalse(result['handled'])
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((root / '.asg-command-protocol').exists())

    def test_declared_protocol_also_routes_through_soc_packaging(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_RUN_DIR': tmp}):
            target = {'pid': os.getpid(), 'create_time': psutil.Process().create_time()}
            for family in ('command_hooks', 'acp'):
                with patch('runtime.protocol_discovery.discover', return_value={'candidates': [
                    {'family': family, 'pointer': '/hooks', 'source': '/unused', 'status': 'configured'}]}):
                    result = fast.attempt(target)
                self.assertFalse(result['handled'])
                self.assertFalse(result['verified'])
                self.assertEqual(result['route'], 'soc_package_pipeline')
                self.assertEqual(result['candidate_count'], 1)

    def test_legacy_callback_does_not_claim_direct_soc_readiness(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_RUN_DIR': tmp}):
            target = {'pid': os.getpid(), 'create_time': psutil.Process().create_time()}
            iid = fast.onboarding.make_instance_id(**target)
            pipeline.save(iid, 'protocol_fastpath', '', status='installed', state_dir='/legacy')
            with patch('runtime.protocol_discovery.discover', return_value={'candidates': []}):
                for _ in range(2):
                    result = fast.attempt(target)
                    self.assertFalse(result['handled'])
                    self.assertEqual(result['legacy_local_install']['state_dir'], '/legacy')
                    self.assertIn('soc_direct_transport', result['missing_checks'])
