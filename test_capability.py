import unittest

from runtime import capability


def states(ladder):
    return {stage["id"]: stage["state"] for stage in ladder["stages"]}


class CapabilityTests(unittest.TestCase):
    def test_untrusted_native_hook_blocks_activation_not_just_waits(self):
        ladder = capability.build(
            {"pid": 123, "create_time": 456.0},
            investigation={"status": "assets_collected", "label": "资产初查完成"},
            install={"status": "installed", "files": [{"path": "/tmp/a.js"}]},
            observation={"target_alive": True, "events": {"valid": 0, "invalid": 0}},
            native_trust={"status": "probed", "owned": {"total": 3, "trusted": 0, "untrusted": 3,
                                                        "by_trust": {"untrusted": 3},
                                                        "duplicate_registrations": [
                                                            {"event": "preToolUse",
                                                             "sources": ["a.toml", "b.json"]}]}})
        result = states(ladder)
        self.assertEqual(result["installed"], "proven")
        self.assertEqual(result["trusted"], "blocked")
        self.assertEqual(result["loaded"], "blocked")
        blocked = [item for item in ladder["blockers"] if item["stage"] == "trusted"]
        self.assertTrue(blocked and blocked[0]["gap"])

    def test_trusted_and_observing_is_proven(self):
        ladder = capability.build(
            {"pid": 1, "create_time": 2.0},
            investigation={"status": "succeeded"},
            install={"status": "installed"},
            observation={"target_alive": True, "events": {"valid": 5, "invalid": 0},
                         "health": {"loaded_observed": True},
                         "blocking": {"status": "unsupported"}},
            native_trust={"status": "probed", "owned": {"total": 1, "trusted": 1, "untrusted": 0}},
            io={"status": "gaps", "capabilities": [{"event": "user.input", "observed": True, "count": 2},
                                                      {"event": "assistant.output", "observed": False, "count": 0}],
                "end_to_end_verified": False})
        result = states(ladder)
        self.assertEqual(result["trusted"], "proven")
        self.assertEqual(result["loaded"], "proven")
        self.assertEqual(result["observing"], "proven")
        self.assertEqual(result["io_coverage"], "pending")
        self.assertEqual(result["controlling"], "unsupported")

    def test_control_requires_real_effect_checks(self):
        base = {"pid": 1, "create_time": 2.0}
        no_effect = capability.build(base, control={"verifications": [{"current": True, "checks": {"decision_returned": True}}]})
        with_effect = capability.build(base, control={"verifications": [
            {"current": True, "checks": {"allow_effect": True, "deny_effect": True, "decision_wait": True}}]})
        self.assertEqual(states(no_effect)["controlling"], "pending")
        self.assertEqual(states(with_effect)["controlling"], "proven")

    def test_stale_instance_verification_does_not_carry_over(self):
        ladder = capability.build({"pid": 9, "create_time": 9.0},
                                  control={"verifications": [{"current": False,
                                                              "checks": {"allow_effect": True, "deny_effect": True}}]})
        self.assertEqual(states(ladder)["controlling"], "not_reached")

    def test_serving_reports_independent_runtime(self):
        not_running = capability.build({"pid": 1, "create_time": 2.0}, serving={"status": "not_running"})
        running = capability.build({"pid": 1, "create_time": 2.0},
                                   serving={"status": "running", "covers_instance": True,
                                            "url": "http://0.0.0.0:8099"})
        self.assertEqual(states(not_running)["serving"], "not_reached")
        self.assertEqual(states(running)["serving"], "pending")

    def test_unknown_trust_is_not_treated_as_passed(self):
        ladder = capability.build({"pid": 1, "create_time": 2.0},
                                  native_trust={"status": "unavailable", "reason": "binary_not_found"})
        self.assertEqual(states(ladder)["trusted"], "unknown")
        self.assertNotIn("trusted", ladder["proven"])

    def test_stages_carry_evidence_time_when_available(self):
        ladder = capability.build(
            {'pid': 5, 'create_time': 1.5},
            native_trust={'status': 'probed', 'probed_at': '2026-09-15T01:00:00Z',
                          'owned': {'total': 1, 'trusted': 1, 'untrusted': 0}},
            observation={'target_alive': True, 'last_event_time': '2026-09-15T02:00:00Z',
                         'events': {'valid': 3, 'invalid': 0}, 'health': {'loaded_observed': True}},
            serving={'status': 'running', 'covers_instance': True, 'started_at': '2026-09-15T00:00:00Z'},
            times={'trusted': '2026-09-15T01:00:00Z', 'observing': '2026-09-15T02:00:00Z',
                   'serving': '2026-09-15T00:00:00Z'})
        by_id = {stage['id']: stage for stage in ladder['stages']}
        self.assertEqual(by_id['trusted']['at'], '2026-09-15T01:00:00Z')
        self.assertEqual(by_id['observing']['at'], '2026-09-15T02:00:00Z')
        self.assertEqual(by_id['serving']['at'], '2026-09-15T00:00:00Z')
        self.assertIsNone(by_id['io_coverage']['at'])

    def test_stages_have_null_time_without_times(self):
        ladder = capability.build({'pid': 5, 'create_time': 1.5})
        self.assertTrue(all('at' in stage and stage['at'] is None for stage in ladder['stages']))


if __name__ == "__main__":
    unittest.main()
