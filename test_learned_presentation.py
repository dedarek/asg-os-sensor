import unittest
from runtime.learned_presentation import hook_state


class LearnedPresentationTests(unittest.TestCase):
    def setUp(self):
        self.target = {'pid': 123, 'create_time': 1000.0}

    def test_no_install_is_not_installed(self):
        state = hook_state(self.target, None, None)
        self.assertEqual(state['status'], 'not_installed')
        self.assertFalse(state['verified'])
        self.assertEqual(state['blocking'], 'not_implemented')

    def test_install_without_verify_is_pending_activation(self):
        install = {'status': 'installed', 'plan_digest': 'a' * 64, 'source': 'learned_install'}
        state = hook_state(self.target, install, None)
        self.assertEqual(state['status'], 'installed_pending_activation')
        self.assertFalse(state['verified'])
        self.assertEqual(state['activation'], 'awaiting_events')

    def test_verify_target_mismatch_does_not_upgrade(self):
        install = {'status': 'installed', 'plan_digest': 'a' * 64}
        verify = {'target': {'pid': 999, 'create_time': 1.0}, 'hook_loaded': True, 'observing': True}
        state = hook_state(self.target, install, verify)
        self.assertEqual(state['status'], 'installed_pending_activation')

    def test_loaded_and_observing(self):
        install = {'status': 'installed', 'plan_digest': 'a' * 64, 'source': 'supervisor_approved_candidate'}
        verify = {'target': dict(self.target), 'hook_loaded': True, 'observing': True,
                  'valid_events': 5, 'invalid_events': 0,
                  'paired_calls': [{'call_id': 'c1', 'tool_name': 'read'}]}
        state = hook_state(self.target, install, verify)
        self.assertEqual(state['status'], 'observing')
        self.assertTrue(state['verified'])
        self.assertTrue(state['observing'])
        self.assertEqual(state['blocking'], 'not_implemented')
        self.assertIn('blocking is not implemented', state['limitations'][0])

    def test_loaded_without_observations(self):
        install = {'status': 'installed', 'plan_digest': 'a' * 64}
        verify = {'target': dict(self.target), 'hook_loaded': True, 'observing': False,
                  'valid_events': 1, 'invalid_events': 0, 'paired_calls': []}
        state = hook_state(self.target, install, verify)
        self.assertEqual(state['status'], 'loaded')
        self.assertTrue(state['verified'])
        self.assertNotIn('observing', state)


if __name__ == '__main__':
    unittest.main()
