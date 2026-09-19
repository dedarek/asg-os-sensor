import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.observation_registry import Registry


class RegistrySourcesTests(unittest.TestCase):
    def test_snapshots_can_limit_reads_to_current_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = Registry(root)
            records = {
                '1:1.0': {'config_path': '/old.json', 'target': {'pid': 1, 'create_time': 1.0}},
                '2:2.0': {'config_path': '/live.json', 'target': {'pid': 2, 'create_time': 2.0}},
            }
            registry.path.write_text(json.dumps(records))
            seen = []

            def load(path):
                seen.append(path)
                return {'target': records['2:2.0']['target']}

            with patch('runtime.observation_registry.observation_source.load_config', side_effect=load), \
                 patch('runtime.observation_registry.observation_source.snapshot', return_value={'status': 'observing'}):
                result = registry.snapshots({'2:2.0'})

            self.assertEqual(seen, ['/live.json'])
            self.assertEqual(result, {'2:2.0': {'status': 'observing'}})

    def test_transport_source_does_not_replace_native_hook_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = Registry(root)
            target = {'pid': 123, 'create_time': 123.5}
            config = {'version': 1, 'target': target,
                      'log_path': str(root / 'native.jsonl'),
                      'fields': {'event': 'event', 'pid': 'pid', 'timestamp': 'timestamp'}}
            original = root / 'native.json'
            original.write_text(json.dumps(config))
            iid = registry.register(original)
            config['log_path'] = str(root / 'transport.jsonl')
            extra = root / 'transport.json'
            extra.write_text(json.dumps(config))
            registry.register(extra, source_name='model-transport')
            records = registry._read()
            self.assertEqual(len(records), 2)
            primary = json.loads(Path(records[iid]['config_path']).read_text())
            self.assertEqual(primary['log_path'], str(root / 'native.jsonl'))
            self.assertEqual(records[iid + '--model-transport']['target'], target)
            with self.assertRaises(ValueError):
                registry.register(extra, source_name='../escape')
