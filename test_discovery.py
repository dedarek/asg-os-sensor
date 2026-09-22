import unittest
import json
import os
import tempfile
import uuid
import plistlib
import psutil
from unittest.mock import Mock
from pathlib import Path

from asg_os_sensor import Sensor, load_policies
from runtime.identity import (identify, load_catalog, ownership, metadata_identity,
                              structural_score, desktop_discovery_candidate,
                              runtime_discovery_candidate, declares_protocol_ecosystem,
                              discovery_probe_allowed)


class Process:
    def __init__(self, name, argv):
        self.info = {'name': name, 'cmdline': argv}

    def children(self, recursive=False):
        return []

    def net_connections(self, kind='inet'):
        return []


class DiscoveryTests(unittest.TestCase):
    def test_profile_combines_independent_signals_and_excludes_argument_values(self):
        p = Mock()
        p.open_files.return_value = [Mock(path='/workspace/AGENTS.md'), Mock(path='/workspace/mcp.json')]
        p.children.return_value = []
        result = runtime_discovery_candidate({'cmdline': ['unknown', '--model', 'private-model', '--approval-mode', 'ask']}, p)
        sources = {s['source'] for s in result['signals']}
        self.assertEqual(sources, {'model-options', 'tool-control-options', 'opened-agent-instructions', 'opened-mcp-configuration'})
        self.assertNotIn('private-model', json.dumps(result))
        self.assertEqual(result['collection']['open_files']['status'], 'collected')
        p.open_files.side_effect = psutil.AccessDenied(1)
        result = runtime_discovery_candidate({'cmdline': ['unknown', '--task', 'private task']}, p)
        self.assertEqual(result['collection']['open_files']['status'], 'unavailable')
        self.assertEqual(result['signals'][0]['source'], 'task-options')

    def test_mcp_child_is_investigation_lead_but_browser_worker_is_not(self):
        p, child = Mock(), Mock()
        p.open_files.return_value = []
        p.children.return_value = [child]
        child.pid = 12
        child.name.return_value = 'node'
        child.cmdline.return_value = ['node', '/packages/@modelcontextprotocol/server-filesystem/index.js']
        self.assertEqual(runtime_discovery_candidate({}, p)['source'], 'mcp-child')
        child.cmdline.return_value = ['browser', '--type=renderer']
        self.assertEqual(runtime_discovery_candidate({}, p), {})
        # Mentioning an agent in a task body never creates a signal.
        self.assertEqual(runtime_discovery_candidate({'cmdline': ['echo', 'agent --model x']}, p), {})

    def test_unknown_package_web_entry_enters_discovery_without_model_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / 'launch.js'
            entry.touch()
            (root / 'package.json').write_text(json.dumps({'name': uuid.uuid4().hex,
                'bin': {'arbitrary': 'launch.js'}}))
            p = Process('node', ['node', str(entry), 'web'])
            self.assertEqual(Sensor(load_policies()).agent_score(p)[0], 0)
            self.assertEqual(runtime_discovery_candidate(p.info, p), {})
            observed = Mock()
            observed.open_files.return_value = [Mock(path=str(root / 'SKILL.md'))]
            observed.children.return_value = []
            self.assertEqual(runtime_discovery_candidate(p.info, observed)['source'], 'package-bin-mapping')

    def test_standard_asset_is_candidate_evidence_without_brand_or_package(self):
        process = Mock()
        process.open_files.return_value = [Mock(path='/example/arbitrary/SKILL.md')]
        self.assertEqual(runtime_discovery_candidate({}, process)['source'], 'opened-standard-asset')
        process.open_files.return_value = [Mock(path='/example/readme.md')]
        process.net_connections.return_value = []
        self.assertEqual(runtime_discovery_candidate({}, process), {})

    def test_owned_package_entry_with_established_connection_is_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / 'launch.js'
            entry.touch()
            (root / 'package.json').write_text(json.dumps({'name': uuid.uuid4().hex,
                'bin': {'arbitrary': 'launch.js'}}))
            p = Process('node', ['node', str(entry), 'run'])
            conn = Mock(status='ESTABLISHED', raddr=Mock(ip='203.0.113.9', port=443))
            idle = Mock()
            idle.open_files.return_value = []
            idle.children.return_value = []
            idle.net_connections.return_value = [Mock(status='LISTEN', raddr=None)]
            self.assertEqual(runtime_discovery_candidate(p.info, idle), {})
            busy = Mock()
            busy.open_files.return_value = []
            busy.children.return_value = []
            busy.net_connections.return_value = [conn]
            result = runtime_discovery_candidate(p.info, busy)
            self.assertIn('model-transport-connection',
                          [s['source'] for s in result['signals']])
            # An established connection alone never admits a process without
            # script-package ownership evidence (browsers, system binaries).
            native = Mock()
            native.open_files.return_value = []
            native.children.return_value = []
            native.net_connections.return_value = [conn]
            self.assertEqual(runtime_discovery_candidate(
                {'cmdline': ['/Applications/SomeApp.app/Contents/MacOS/SomeApp']}, native), {})


    def test_protocol_ecosystem_dependency_admits_investigation_generically(self):
        # A previously unseen harness that embeds an MCP/ACP client stack must
        # enter role investigation even with no model SDK, no CLI flags, no
        # children and no open standard assets. The signal names no vendor.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / 'bin' / 'mystery.js'
            entry.parent.mkdir()
            entry.touch()
            (root / 'package.json').write_text(json.dumps({
                'name': 'unseen-runtime-' + uuid.uuid4().hex,
                'bin': {'mystery': 'bin/mystery.js'},
                'dependencies': {'some-org-mcp-client': '*', 'commander': '*'}}))
            p = Process('node', ['node', str(entry), 'web'])
            # Score stays at zero: a declared dependency never asserts Agent.
            self.assertEqual(Sensor(load_policies()).agent_score(p)[0], 0)
            result = runtime_discovery_candidate(p.info, p)
            sources = {s['source'] for s in result['signals']}
            self.assertIn('protocol-ecosystem-dependency', sources)
        # Whole-token matching only: lookalike names never match.
        self.assertEqual(declares_protocol_ecosystem(['mcpkit', 'acpx-tools',
            'modelkit']), [])
        self.assertEqual(declares_protocol_ecosystem(['@x/mcp-client']), ['@x/mcp-client'])
        # A plain package with no protocol stack stays out of investigation.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / 'bin' / 'plain.js'
            entry.parent.mkdir()
            entry.touch()
            (root / 'package.json').write_text(json.dumps({
                'name': 'plain-cli-' + uuid.uuid4().hex,
                'bin': {'plain': 'bin/plain.js'},
                'dependencies': {'commander': '*'}}))
            p = Process('node', ['node', str(entry), 'serve'])
            self.assertEqual(runtime_discovery_candidate(p.info, p), {})

    def test_opaque_desktop_main_is_discoverable_without_agent_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / (uuid.uuid4().hex + '.app')
            exe = bundle / 'Contents' / 'MacOS' / 'opaque'
            exe.parent.mkdir(parents=True)
            exe.touch()
            manifest = bundle / 'Contents' / 'Info.plist'
            manifest.write_bytes(plistlib.dumps({'CFBundleExecutable': 'opaque', 'CFBundleName': 'Arbitrary'}))
            p = Process('opaque', [str(exe)])
            p.info['exe'] = str(exe)
            self.assertEqual(Sensor(load_policies()).agent_score(p)[0], 0)
            self.assertTrue(desktop_discovery_candidate(p.info))
            self.assertEqual(runtime_discovery_candidate(p.info, p), {})
            helper = bundle / 'Contents' / 'Frameworks' / 'worker'
            helper.parent.mkdir()
            helper.touch()
            self.assertFalse(desktop_discovery_candidate({'exe': str(helper)}))
            manifest.write_bytes(plistlib.dumps({'CFBundleExecutable': 'opaque', 'LSBackgroundOnly': True}))
            self.assertFalse(desktop_discovery_candidate(p.info))

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

    def test_symlink_entry_uses_real_package_bin_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_root = root / 'node_modules' / 'runtime-package'
            target = package_root / 'bin' / 'runtime.exe'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'fixture executable')
            (package_root / 'package.json').write_text(json.dumps({
                'name': 'runtime-package', 'version': '4.5.6',
                'bin': {'runtime-package': 'bin/runtime.exe'},
            }))
            launch = root / 'runtime-link.exe'
            launch.symlink_to(target)
            meta = metadata_identity({
                'exe': str(launch), 'name': 'runtime-link.exe',
                'cmdline': [str(launch)],
            })
            self.assertEqual(meta['name'], 'runtime-package')
            self.assertEqual(meta['ownership'], 'bin-mapping')
            self.assertEqual(meta['entrypoint'], str(launch))
            self.assertEqual(meta['resolved_entrypoint'], str(target.resolve()))

    def test_unrelated_parent_package_is_not_native_entry_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'child' / 'bin' / 'runtime.exe'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'fixture executable')
            (root / 'package.json').write_text(json.dumps({
                'name': 'unrelated-toolchain', 'bin': {'other': 'bin/other.exe'},
            }))
            meta = metadata_identity({
                'exe': str(target), 'name': 'runtime.exe',
                'cmdline': [str(target)],
            })
            self.assertEqual(meta, {})

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

    def test_system_daemon_skips_expensive_discovery_probes(self):
        p = Mock()
        p.info = {'pid': 99, 'name': 'ordinaryd', 'exe': '/usr/libexec/ordinaryd',
                  'cmdline': ['/usr/libexec/ordinaryd']}
        p.children.side_effect = AssertionError('system daemon must not be deeply probed')
        self.assertFalse(discovery_probe_allowed(p.info))
        self.assertEqual(Sensor(load_policies()).agent_score(p)[0], 0)

    def test_user_installed_and_protocol_flagged_entries_are_probeable(self):
        self.assertTrue(discovery_probe_allowed({
            'exe': '/Applications/Unknown.app/Contents/MacOS/Unknown',
            'cmdline': ['/Applications/Unknown.app/Contents/MacOS/Unknown'],
        }))
        self.assertTrue(discovery_probe_allowed({
            'exe': '/usr/bin/python3',
            'cmdline': ['/usr/bin/python3', 'worker.py', '--model', 'local'],
        }))

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
