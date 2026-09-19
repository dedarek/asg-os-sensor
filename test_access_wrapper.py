"""Alternative-access wrapper invariants."""
import unittest

from runtime import access_wrapper


class AccessWrapperTests(unittest.TestCase):
    def test_wrap_environment_rewrites_only_provider_keys(self):
        env = {'OPENAI_BASE_URL': 'https://api.example/v1', 'ANTHROPIC_BASE_URL': 'https://a',
               'PATH': '/usr/bin', 'HOME': '/Users/x'}
        result = access_wrapper.wrap_environment(env, wrapper_base='http://127.0.0.1:9/v1')
        self.assertEqual(set(result['changed']), {'OPENAI_BASE_URL', 'ANTHROPIC_BASE_URL'})
        self.assertEqual(result['env']['PATH'], '/usr/bin')
        self.assertEqual(result['env']['HOME'], '/Users/x')
        self.assertTrue(all(result['env'][k] == 'http://127.0.0.1:9/v1'
                            for k in result['changed']))

    def test_wrap_environment_adds_default_when_absent(self):
        result = access_wrapper.wrap_environment({'PATH': '/usr/bin'}, wrapper_base='http://w/v1')
        self.assertEqual(result['changed'], ['OPENAI_BASE_URL(default)'])
        self.assertEqual(result['env']['OPENAI_BASE_URL'], 'http://w/v1')

    def test_payload_truncation_is_marked(self):
        small = access_wrapper._payload({'a': 1})
        self.assertFalse(small['truncated'])
        big = access_wrapper._payload('x' * (access_wrapper.MAX_FIELD_BYTES + 10))
        self.assertTrue(big['truncated'])
        self.assertEqual(big['truncation']['limit_bytes'], access_wrapper.MAX_FIELD_BYTES)

    def test_events_use_contract_canonical_names(self):
        for name in ('model.request', 'model.response', 'control.applied'):
            self.assertEqual(access_wrapper.canonical_event(name), name)


if __name__ == '__main__':
    unittest.main()
