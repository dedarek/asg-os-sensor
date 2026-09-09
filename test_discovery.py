import unittest
import json
import tempfile
import uuid
from pathlib import Path

from asg_os_sensor import Sensor, load_policies
from runtime.identity import identify, load_catalog, ownership, metadata_identity, structural_score


class Process:
    def __init__(self, name, argv):
        self.info = {'name': name, 'cmdline': argv}

    def children(self, recursive=False):
        return []

    def net_connections(self, kind='inet'):
        return []


class DiscoveryTests(unittest.TestCase):
    def test_unseen_package_without_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = 'runtime-' + uuid.uuid4().hex
            root = Path(tmp)
            (root / 'package.json').write_text(json.dumps({'name': name, 'dependencies': {'openai': '*'}}))
            p = Process('node', ['node', str(root / 'index.js')])
            meta = metadata_identity(p.info)
            self.assertEqual(meta['name'], name)
            self.assertFalse(identify(p.info, {'agents': []}))
            score, _ = structural_score(p.info, [{'name': 'worker', 'cmdline': ['worker']}], meta)
            self.assertGreaterEqual(score, 50)

    def test_unseen_cli_host_and_negative_control(self):
        name = uuid.uuid4().hex
        child = {'name': name + '-cli', 'cmdline': [name + '-cli']}
        score, _ = structural_score({}, [child], {'name': name})
        self.assertGreaterEqual(score, 50)
        # Ordinary Electron workers / network activity are not Agent evidence.
        score, _ = structural_score({}, [{'name': 'helper', 'cmdline': ['helper']}], {'name': name})
        self.assertEqual(score, 0)

    def test_idle_native_and_interpreter_identity(self):
        catalog = load_catalog()
        cases = [(['codex'], 'codex'), (['zcode-cli'], 'zcode'),
                 (['/opt/bun.exe', '/Users/mac/opencodex/src/cli/index.ts', 'start'], 'opencodex'),
                 (['python3', '-m', 'hermes_cli.main'], 'hermes')]
        sensor = Sensor(load_policies())
        for argv, expected in cases:
            with self.subTest(argv=argv):
                p = Process(argv[0], argv)
                self.assertEqual(identify(p.info, catalog)['id'], expected)
                self.assertGreaterEqual(sensor.agent_score(p)[0], 50)

    def test_prompt_is_not_identity(self):
        p = Process('python3', ['python3', 'report.py', 'inspect opencode and claude-code'])
        self.assertFalse(identify(p.info, load_catalog()))
        self.assertEqual(Sensor(load_policies()).agent_score(p)[0], 0)

    def test_unknown_behavior_still_detected(self):
        p = Process('python3', ['python3', 'novel.py', '--model', 'local', '--system-prompt', 'inspect'])
        self.assertFalse(identify(p.info, load_catalog()))
        self.assertGreaterEqual(Sensor(load_policies()).agent_score(p)[0], 50)

    def test_shell_and_helpers_excluded(self):
        s = Sensor(load_policies())
        for p in [Process('zsh', ['zsh', '-c', 'codex --model x']),
                  Process('OpenCode Helper', ['helper', '--type=renderer'])]:
            self.assertLess(s.agent_score(p)[0], 0)

    def test_ownership_preserves_nested_different_agents(self):
        snapshot = {1: {'ppid': 0}, 2: {'ppid': 1}, 3: {'ppid': 2},
                    4: {'ppid': 2}, 5: {'ppid': 4}, 6: {'ppid': 0}}
        identities = {1: {'id': 'codex'}, 3: {'id': 'codex'}, 4: {'id': 'claude'}, 6: {'id': 'codex'}}
        self.assertEqual(ownership(snapshot, identities, {1, 3, 4, 6}),
                         {1: [1, 2, 3], 4: [4, 5], 6: [6]})

    def test_unrelated_unknown_roots_not_merged(self):
        self.assertEqual(ownership({1: {'ppid': 0}, 2: {'ppid': 0}}, {}, {1, 2}),
                         {1: [1], 2: [2]})


if __name__ == '__main__':
    unittest.main()
