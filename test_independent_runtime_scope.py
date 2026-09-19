"""The standalone Hook runtime must not depend on the discovery pipeline."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DISCOVERY_HINTS = ('analyzer', 'onboard', 'investigat', 'goose', 'autonomous',
                   'find_agents', 'matcher', 'llm_proxy')


def _runtime_local_closure(entry):
    seen, stack, names = set(), [entry], set()
    while stack:
        rel = stack.pop()
        if rel in seen or not (ROOT / rel).is_file():
            continue
        seen.add(rel)
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        for name in list(names):
            if name.startswith('runtime.'):
                candidate = 'runtime/' + name.split('.', 1)[1].split('.')[0] + '.py'
                if (ROOT / candidate).is_file():
                    stack.append(candidate)
    return seen, names


class IndependentRuntimeScopeTests(unittest.TestCase):
    def test_hook_service_closure_has_no_discovery_module(self):
        seen, names = _runtime_local_closure('runtime/hook_service.py')
        offending = sorted(n for n in names if any(h in n.lower() for h in DISCOVERY_HINTS))
        self.assertEqual(offending, [], 'standalone service pulls in discovery modules: %s' % offending)
        self.assertIn('runtime/hook_service.py', seen)

    def test_hook_launcher_supports_lifecycle_and_identity_check(self):
        source = (ROOT / 'hook_runtime.py').read_text()
        for action in ('start', 'stop', 'status'):
            self.assertIn(action, source)
        # The launcher must verify process identity, not trust a bare pid.
        self.assertIn('create_time', source)
        self.assertIn('hook_runtime.py', source)


if __name__ == '__main__':
    unittest.main()
