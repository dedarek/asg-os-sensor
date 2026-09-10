import io
import json
import unittest
from unittest.mock import patch
from runtime import analyst_tools as at


class ProposalPhaseTests(unittest.TestCase):
    def test_phase_advertises_only_selected_tools(self):
        request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n'
        output = io.StringIO()
        with patch.dict('os.environ', {'ASG_ANALYST_TOOL_ALLOWLIST': 'propose_recipe,read_evidence'}), \
             patch('sys.stdin', io.StringIO(request)), patch('sys.stdout', output):
            at.main()
        advertised = json.loads(output.getvalue())['result']['tools']
        self.assertEqual({t['name'] for t in advertised}, {'propose_recipe', 'read_evidence'})
        recipe = next(t for t in advertised if t['name'] == 'propose_recipe')['inputSchema']['properties']['recipe']
        self.assertIn('content', recipe['properties']['install_plan']['properties']['files']['items']['properties'])
        self.assertIn('investigation', recipe['required'])

    def test_unknown_phase_tool_is_config_error(self):
        with patch.dict('os.environ', {'ASG_ANALYST_TOOL_ALLOWLIST': 'invented_tool'}):
            with self.assertRaises(ValueError):
                at.main()
