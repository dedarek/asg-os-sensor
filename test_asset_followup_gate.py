# -*- coding: utf-8 -*-
"""Asset-phase follow-up for an exact-matched instance.

exact reuse must keep reusing the Hook recipe, yet a new instance still needs
its own asset snapshot. The gate only declares intent; duplicate prevention is
already enforced by the queue, which is tested here against the real function.
"""
import os
import unittest
from unittest.mock import patch

import monitor_dashboard as dashboard


class AssetFollowupGateTests(unittest.TestCase):
    def gate(self, *, matched=True, result=None, assets_phase=True):
        return dashboard._needs_asset_followup(
            is_matched=matched, investigation_result=result, assets_phase=assets_phase)

    def test_exact_match_without_assets_is_scheduled(self):
        for result in (None, {}, {"status": "running"}, {"status": "failed"},
                       {"status": "not_scheduled"}):
            with self.subTest(result=result):
                self.assertTrue(self.gate(result=result))

    def test_existing_asset_checkpoint_is_not_rescheduled(self):
        for status in ("assets_collected", "partial", "succeeded", "reused"):
            with self.subTest(status=status):
                self.assertFalse(self.gate(result={"status": status}))

    def test_non_assets_phase_keeps_exact_reuse_as_before(self):
        self.assertFalse(self.gate(result={}, assets_phase=False))
        self.assertFalse(self.gate(result=None, assets_phase=False))

    def test_unmatched_instance_is_left_to_the_existing_path(self):
        # A miss already schedules through `not is_matched`; the helper adds nothing.
        self.assertFalse(self.gate(matched=False, result={}))
        self.assertFalse(self.gate(matched=False, result=None))


class QueueDeduplicationTests(unittest.TestCase):
    """Prove the gate cannot cause a repeat investigation, using the real queue."""

    def setUp(self):
        self.instance_id = "4242:1789000000.5"
        self._snapshot = {
            "instances": dict(dashboard.INVESTIGATING_INSTANCES),
            "queued": dict(dashboard.INVESTIGATION_QUEUED),
            "queue": list(dashboard.INVESTIGATION_QUEUE),
            "results": dict(dashboard.INVESTIGATION_RESULTS),
            "retry": dict(dashboard.INVESTIGATION_RETRY_AT),
        }
        with dashboard.INVESTIGATION_LOCK:
            dashboard.INVESTIGATING_INSTANCES.pop(self.instance_id, None)
            dashboard.INVESTIGATION_QUEUED.pop(self.instance_id, None)
            dashboard.INVESTIGATION_QUEUE.clear()
            dashboard.INVESTIGATION_RESULTS.pop(self.instance_id, None)
            dashboard.INVESTIGATION_RETRY_AT.pop(self.instance_id, None)

    def tearDown(self):
        with dashboard.INVESTIGATION_LOCK:
            dashboard.INVESTIGATING_INSTANCES.clear()
            dashboard.INVESTIGATING_INSTANCES.update(self._snapshot["instances"])
            dashboard.INVESTIGATION_QUEUED.clear()
            dashboard.INVESTIGATION_QUEUED.update(self._snapshot["queued"])
            dashboard.INVESTIGATION_QUEUE.clear()
            dashboard.INVESTIGATION_QUEUE.extend(self._snapshot["queue"])
            dashboard.INVESTIGATION_RESULTS.clear()
            dashboard.INVESTIGATION_RESULTS.update(self._snapshot["results"])
            dashboard.INVESTIGATION_RETRY_AT.clear()
            dashboard.INVESTIGATION_RETRY_AT.update(self._snapshot["retry"])

    def enqueue(self, status=None):
        # Never hold INVESTIGATION_LOCK here: _enqueue_investigation acquires it
        # itself, and the lock is not reentrant.
        if status is not None:
            with dashboard.INVESTIGATION_LOCK:
                dashboard.INVESTIGATION_RESULTS[self.instance_id] = {"status": status}
        return dashboard._enqueue_investigation(
            4242, self.instance_id, 1789000000.5)

    def test_first_asset_phase_followup_enqueues_once(self):
        self.assertEqual(self.gate_and_enqueue(), "enqueued")
        # The same instance is now queued, so a second scan must not duplicate it.
        self.assertEqual(self.gate_and_enqueue(), "duplicate")

    def gate_and_enqueue(self):
        # The gate is what the scan loop checks before calling the scheduler.
        self.assertTrue(dashboard._needs_asset_followup(
            is_matched=True, investigation_result=None, assets_phase=True))
        return self.enqueue()

    def test_existing_asset_result_blocks_a_repeat(self):
        for status in ("assets_collected", "partial", "succeeded", "reused"):
            with self.subTest(status=status):
                with dashboard.INVESTIGATION_LOCK:
                    dashboard.INVESTIGATION_QUEUED.pop(self.instance_id, None)
                    dashboard.INVESTIGATION_QUEUE.clear()
                    dashboard.INVESTIGATION_RESULTS.pop(self.instance_id, None)
                    dashboard.INVESTIGATION_RETRY_AT.pop(self.instance_id, None)
                # The gate would allow a new attempt, but a stored checkpoint is
                # already present, so the queue must refuse it.
                self.assertFalse(dashboard._needs_asset_followup(
                    is_matched=True, investigation_result={"status": status},
                    assets_phase=True))
                self.assertEqual(self.enqueue(status=status), "duplicate")

    def test_active_run_blocks_a_repeat(self):
        with dashboard.INVESTIGATION_LOCK:
            dashboard.INVESTIGATING_INSTANCES[self.instance_id] = 1789000000.5
        try:
            self.assertEqual(self.enqueue(), "duplicate")
        finally:
            with dashboard.INVESTIGATION_LOCK:
                dashboard.INVESTIGATING_INSTANCES.pop(self.instance_id, None)

    def test_retry_cooldown_blocks_a_repeat(self):
        with dashboard.INVESTIGATION_LOCK:
            dashboard.INVESTIGATION_RETRY_AT[self.instance_id] = dashboard.now() + 300
        try:
            self.assertEqual(self.enqueue(), "duplicate")
        finally:
            with dashboard.INVESTIGATION_LOCK:
                dashboard.INVESTIGATION_RETRY_AT.pop(self.instance_id, None)

    def test_env_phase_gates_the_scheduling_branch(self):
        with patch.dict(os.environ, {"ASG_INVESTIGATION_PHASE": "assets"}):
            self.assertTrue(dashboard._needs_asset_followup(
                is_matched=True, investigation_result=None,
                assets_phase=os.environ.get("ASG_INVESTIGATION_PHASE", "").strip() == "assets"))
        with patch.dict(os.environ, {"ASG_INVESTIGATION_PHASE": ""}):
            self.assertFalse(dashboard._needs_asset_followup(
                is_matched=True, investigation_result=None,
                assets_phase=os.environ.get("ASG_INVESTIGATION_PHASE", "").strip() == "assets"))


if __name__ == "__main__":
    unittest.main()
