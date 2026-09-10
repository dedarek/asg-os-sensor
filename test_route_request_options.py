import unittest
from unittest.mock import patch
from runtime.llm_proxy import apply_request_options


class ApplyRequestOptionsTests(unittest.TestCase):
    def test_transparent_options_pass_through(self):
        payload = {'model': 'qwen38-27b', 'messages': [{'role': 'user', 'content': 'hi'}]}
        options = {'chat_template_kwargs': {'enable_thinking': False},
                   'reasoning_effort': 'low'}
        result = apply_request_options(payload, options)
        self.assertEqual(result['chat_template_kwargs'], {'enable_thinking': False})
        self.assertEqual(result['reasoning_effort'], 'low')
        self.assertEqual(result['messages'], payload['messages'])
        self.assertEqual(result['model'], 'qwen38-27b')

    def test_original_payload_not_mutated(self):
        payload = {'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True}
        original = json_copy(payload)
        options = {'chat_template_kwargs': {'enable_thinking': False}}
        result = apply_request_options(payload, options)
        self.assertEqual(payload, original)
        self.assertIsNot(result, payload)

    def test_forbidden_fields_rejected(self):
        payload = {'model': 'm', 'messages': []}
        for field in ('messages', 'tools', 'model', 'stream', 'tool_choice'):
            with self.assertRaises(ValueError):
                apply_request_options(payload, {field: 'x'})

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            apply_request_options('not a dict', {})
        with self.assertRaises(ValueError):
            apply_request_options({}, 'not a dict')
        with self.assertRaises(ValueError):
            apply_request_options(None, {})

    def test_empty_options_unchanged(self):
        payload = {'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}]}
        result = apply_request_options(payload, {})
        self.assertEqual(result, payload)
        self.assertIsNot(result, payload)


def json_copy(value):
    import copy
    return copy.deepcopy(value)


if __name__ == '__main__':
    unittest.main()
