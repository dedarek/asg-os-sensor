"""The portability audit must run after release destinations exist."""
import json
import tempfile
import unittest
from pathlib import Path

from release import build_release


class ReleaseBuildOrderTests(unittest.TestCase):
    def test_sanitizer_writes_audit_and_readme_before_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / 'app/runtime').mkdir(parents=True)
            (package / 'release').mkdir()
            (package / 'README.md').write_text('ASG\n')
            database = {'fingerprints': [
                {'id': 'local', 'hook_recipe': {'workspace': str(Path.home() / 'private')}},
                {'id': 'portable', 'features': {'runtime': 'node'}}]}
            (package / 'app/runtime/fingerprints.json').write_text(json.dumps(database))
            build_release.sanitize_portability(package)
            stored = json.loads((package / 'app/runtime/fingerprints.json').read_text())
            audit = json.loads((package / 'release/portability-scan.json').read_text())
            self.assertEqual([item['id'] for item in stored['fingerprints']], ['portable'])
            self.assertEqual([item['id'] for item in audit['fingerprints_dropped']], ['local'])
            self.assertIn('指纹库可移植性', (package / 'README.md').read_text())
            self.assertIn('release/portability-scan.json', build_release.manifest_files(package))


if __name__ == '__main__':
    unittest.main()
