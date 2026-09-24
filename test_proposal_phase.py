import io
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from runtime import analyst_tools as at


class ProposalPhaseTests(unittest.TestCase):
    def test_narrow_container_normalization_does_not_edit_source(self):
        recipe = {'match_features': {'runtime': 'fixture'}, 'observation': 'fixture',
                  'hook': {'method': 'file_plan'}, 'fallback': 'none',
                  'install_plan': {'files': [{'content': 'export default () => ({text: "x"});\n'}]}}
        raw = json.dumps(recipe)
        with tempfile.TemporaryDirectory() as tmp, patch.object(at, 'target_process'), \
             patch.object(at, 'RECIPE_DIR', Path(tmp)), patch.object(at, '_validate_investigation_summary'), \
             patch('runtime.recipe_validation.validate'):
            result = at.call_tool('propose_recipe', {'recipe': raw[:-1]})
            self.assertEqual(result['recipe'], recipe)
            stored = json.loads((Path(tmp) / 'candidate.json').read_text())
            self.assertIn('missing_final_object_brace', stored['transport_normalization'])
            with self.assertRaises(json.JSONDecodeError):
                at.call_tool('propose_recipe', {'recipe': '{"hook": "unfinished'})

    def test_duplicated_trailing_object_braces_are_stripped(self):
        recipe = {'match_features': {'runtime': 'fixture'}, 'observation': 'fixture',
                  'hook': {'method': 'unsupported'}, 'fallback': 'none'}
        raw = json.dumps(recipe) + '}}'
        with tempfile.TemporaryDirectory() as tmp, patch.object(at, 'target_process'), \
             patch.object(at, 'RECIPE_DIR', Path(tmp)), patch.object(at, '_validate_investigation_summary'), \
             patch('runtime.recipe_validation.validate'):
            result = at.call_tool('propose_recipe', {'recipe': raw})
            self.assertEqual(result['recipe'], recipe)
            stored = json.loads((Path(tmp) / 'candidate.json').read_text())
            self.assertIn('trailing_object_braces_stripped', stored['transport_normalization'])

    def test_extra_data_with_real_content_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(at, 'target_process'), \
             patch.object(at, 'RECIPE_DIR', Path(tmp)), patch.object(at, '_validate_investigation_summary'), \
             patch('runtime.recipe_validation.validate'):
            with self.assertRaises(json.JSONDecodeError):
                at.call_tool('propose_recipe', {'recipe': '{"observation": "a"} {"observation": "b"}'})
            with self.assertRaises(json.JSONDecodeError):
                at.call_tool('propose_recipe', {'recipe': '{"observation": "a"}} garbage'})

    def test_executable_recipe_requires_top_level_plan(self):
        from runtime.recipe_validation import validate
        recipe = {'agent_identity_name': 'fixture', 'match_features': {'runtime': 'fixture'},
                  'observation': 'fixture', 'fallback': 'leave untouched', 'evidence_refs': ['ev-fixture'],
                  'hook': {'method': 'file_plan', 'restart_required': True, 'capabilities': [],
                           'limitations': [], 'verification': 'fixture', 'rollback': 'fixture'}}
        with self.assertRaisesRegex(ValueError, 'top level'):
            validate(recipe, Path('.'))

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
