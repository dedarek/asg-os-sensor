import hashlib
import io
import json
import platform
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from integrations.soc_inventory.deployment_package import build
from runtime.protocol_package import prepare


class FakeProcess:
    def __init__(self, workspace):
        self.pid = 123
        self.workspace = Path(workspace)

    def cwd(self): return str(self.workspace)
    def exe(self): return str(Path(sys.executable).resolve())
    def cmdline(self): return [self.exe()]


class ProtocolPackageTests(unittest.TestCase):
    def compatibility(self):
        exe = Path(sys.executable).resolve()
        return {'platform': platform.system(), 'architecture': platform.machine(),
                'runtime': 'native', 'entry': 'native',
                'executable': hashlib.sha256(exe.read_bytes()).hexdigest()}

    def test_directory_candidate_never_generates_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            process = FakeProcess(tmp)
            with patch('runtime.protocol_package.discover', return_value={'candidates': [{
                    'family': 'command_hooks', 'pointer': '/hooks',
                    'source': str(Path(tmp) / 'settings.json'),
                    'events': ['PreToolUse'], 'loaded_by_process': False}]}):
                self.assertIsNone(prepare(process))

    def test_opened_json_becomes_direct_soc_package_and_real_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); workspace = root / 'workspace'; workspace.mkdir()
            config = workspace / 'settings.json'
            config.write_text(json.dumps({'theme': 'preserve', 'hooks': {
                'UserPromptSubmit': [{'type': 'command', 'command': 'existing'}],
                'PreToolUse': [{'type': 'command', 'command': 'existing'}]}}))
            process = FakeProcess(workspace)
            candidate = {'family': 'command_hooks', 'pointer': '/hooks',
                         'source': str(config), 'events': ['UserPromptSubmit', 'PreToolUse'],
                         'loaded_by_process': True}
            with patch('runtime.protocol_package.discover', return_value={'candidates': [candidate]}), \
                 patch('runtime.protocol_package.observe', return_value=self.compatibility()):
                bundle = prepare(process)
            self.assertEqual(bundle['verification']['recipe_source'], 'deterministic_protocol')
            self.assertFalse(bundle['verification']['hook_verified'])
            archive = build(bundle)
            package = root / 'package'; package.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(package)
            token = root / 'agent.key'; token.write_text('isolated-test-key')
            install = subprocess.run([sys.executable, str(package / 'install.py'),
                '--target', str(workspace), '--exe', str(Path(sys.executable).resolve()),
                '--platform', 'unknown-agent', '--backend-url', 'http://127.0.0.1:1',
                '--agent-id', 'protocol-test', '--token-file', str(token)],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(install.returncode, 0, install.stderr)
            saved = json.loads(config.read_text())
            self.assertEqual(saved['theme'], 'preserve')
            self.assertEqual(saved['hooks']['UserPromptSubmit'][0]['command'], 'existing')
            self.assertEqual(len(saved['hooks']['UserPromptSubmit']), 2)
            runner = workspace / '.asg-soc-hook/command_hook.py'
            callback = subprocess.run([sys.executable, str(runner)], input=json.dumps({
                'hook_event_name': 'UserPromptSubmit', 'session_id': 's',
                'prompt': '真实输入'}), capture_output=True, text=True, timeout=20)
            self.assertEqual(callback.returncode, 0, callback.stderr)
            rows = [json.loads(line) for line in
                    (workspace / '.asg-soc-hook/events.jsonl').read_text().splitlines()]
            self.assertEqual(rows[-1]['event'], 'user.input')
            self.assertEqual(rows[-1]['content'], '真实输入')
            self.assertTrue(any((workspace / '.soc-hook/soc-outbox').glob('*.sqlite3')))


if __name__ == '__main__': unittest.main()
