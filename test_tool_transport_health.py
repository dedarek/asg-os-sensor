import unittest
from runtime.tool_transport_health import ToolTransportHealth


class ToolTransportHealthTests(unittest.TestCase):
    def test_never_alive_false_does_not_report_lost(self):
        h = ToolTransportHealth(threshold=3)
        for _ in range(5):
            self.assertEqual(h.observe(False), 'never_alive')

    def test_none_is_unknown_not_death(self):
        h = ToolTransportHealth(threshold=3)
        self.assertEqual(h.observe(None), 'unknown')
        h.observe(True)
        self.assertEqual(h.observe(None), 'alive')
        self.assertEqual(h.observe(None), 'alive')

    def test_alive_then_consecutive_false_reaches_transport_lost(self):
        h = ToolTransportHealth(threshold=3)
        h.observe(True)
        self.assertEqual(h.observe(False), 'alive')
        self.assertEqual(h.observe(False), 'alive')
        self.assertEqual(h.observe(False), 'transport_lost')

    def test_recovery_resets_counter(self):
        h = ToolTransportHealth(threshold=3)
        h.observe(True)
        h.observe(False)
        h.observe(False)
        h.observe(True)
        self.assertEqual(h.observe(False), 'alive')
        self.assertEqual(h.observe(False), 'alive')
        self.assertEqual(h.observe(False), 'transport_lost')


if __name__ == '__main__':
    unittest.main()
