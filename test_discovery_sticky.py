import unittest

from runtime import discovery_sticky


class DiscoveryStickyTests(unittest.TestCase):
    def test_bursty_instance_keeps_evidence_until_ttl(self):
        cache = {}
        evidence = {'source': 'model-transport-connection', 'signals': [], 'collection': {}}
        now = 1000.0
        self.assertIs(discovery_sticky.apply(cache, 7, 55.0, evidence, now=now), evidence)
        kept = discovery_sticky.apply(cache, 7, 55.0, {}, now=now + 60)
        self.assertEqual(kept['retained_for'], 'recent-instance-evidence')
        self.assertEqual(discovery_sticky.apply(cache, 7, 55.0, {}, now=now + 400), {})
        # PID reuse must drop the previous instance immediately.
        discovery_sticky.apply(cache, 8, 10.0, evidence, now=now)
        self.assertEqual(discovery_sticky.apply(cache, 8, 99.0, {}, now=now + 1), {})
        # prune only removes expired entries.
        discovery_sticky.apply(cache, 9, 10.0, evidence, now=now)
        discovery_sticky.prune(cache, now=now + 301)
        self.assertNotIn(9, cache)
        self.assertNotIn(7, cache)
