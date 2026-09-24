"""Every third-party module imported by bundled code must ship in the release."""
import ast
import unittest
from pathlib import Path

from release import build_release

ROOT = Path(__file__).resolve().parent

# top-level import name -> pip distribution that provides it
THIRD_PARTY = {'psutil': 'psutil', 'yaml': 'pyyaml', 'tomlkit': 'tomlkit',
               'json5': 'json5', 'dotenv': 'python-dotenv', 'requests': 'requests',
               'google': 'protobuf', 'opentelemetry': 'opentelemetry-proto'}


def top_level_imports(source):
    found = set()
    try:
        tree = ast.parse(source.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError):
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split('.')[0])
    return found


class ReleaseDependenciesTests(unittest.TestCase):
    def test_bundled_imports_are_declared(self):
        needed = set()
        for entry in build_release.APP_ENTRIES:
            path = ROOT / entry
            if not path.exists():
                continue
            files = path.rglob('*.py') if path.is_dir() else [path]
            for source in files:
                needed |= top_level_imports(source)
        missing = {THIRD_PARTY[name] for name in needed if name in THIRD_PARTY}
        undeclared = missing - set(build_release.DEPENDENCIES)
        self.assertEqual(sorted(undeclared), [],
                         'release DEPENDENCIES missing: %s' % sorted(undeclared))


if __name__ == '__main__':
    unittest.main()
