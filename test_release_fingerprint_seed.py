import json
import tempfile
import unittest
from pathlib import Path

from release.asgctl import seed_fingerprint_db


class ReleaseFingerprintSeedTests(unittest.TestCase):
    def test_first_install_seeds_and_upgrade_repairs_shipped_recipe(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, release = root / 'home', root / 'release'
            (release / 'app/runtime').mkdir(parents=True)
            shipped = {'version': 1, 'fingerprints': [
                {'id': 'official', 'hook_recipe': {'value': 'fixed'}, 'match_count': 0}]}
            (release / 'app/runtime/fingerprints.json').write_text(json.dumps(shipped))
            first = seed_fingerprint_db(home, release)
            self.assertEqual(first['count'], 1)

            target = home / 'state/fingerprints.json'
            current = json.loads(target.read_text())
            current['fingerprints'][0].update(hook_recipe={'value': 'broken'}, match_count=9)
            current['fingerprints'].append({'id': 'local', 'hook_recipe': {'value': 'learned'}})
            target.write_text(json.dumps(current))

            merged = seed_fingerprint_db(home, release)
            by_id = {item['id']: item for item in json.loads(target.read_text())['fingerprints']}
            self.assertEqual(merged['count'], 2)
            self.assertEqual(by_id['official']['hook_recipe']['value'], 'fixed')
            self.assertEqual(by_id['official']['match_count'], 9)
            self.assertEqual(by_id['local']['hook_recipe']['value'], 'learned')

    def test_corrupt_runtime_db_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, release = root / 'home', root / 'release'
            (release / 'app/runtime').mkdir(parents=True)
            (release / 'app/runtime/fingerprints.json').write_text(
                json.dumps({'version': 1, 'fingerprints': []}))
            target = home / 'state/fingerprints.json'
            target.parent.mkdir(parents=True)
            target.write_text('{broken')
            with self.assertRaises(ValueError):
                seed_fingerprint_db(home, release)
            self.assertEqual(target.read_text(), '{broken')


if __name__ == '__main__':
    unittest.main()
