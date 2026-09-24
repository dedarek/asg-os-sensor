
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release import asgctl


class LegacyCollectorRetirementTests(unittest.TestCase):
    def test_retire_removes_leftover_collector_plist(self):
        with tempfile.TemporaryDirectory() as directory:
            agents = Path(directory) / 'LaunchAgents'
            agents.mkdir()
            leftover = agents / (asgctl.LEGACY_COLLECTOR_LABEL + '.plist')
            leftover.write_text('stub')
            calls = []

            def fake_run(args, *a, **k):
                calls.append(list(args))
                return subprocess.CompletedProcess(args, 0)

            with mock.patch.object(asgctl, 'plist_path',
                                   lambda label: agents / (label + '.plist')), \
                 mock.patch.object(subprocess, 'run', side_effect=fake_run):
                asgctl._retire_legacy_collector_service()
            self.assertFalse(leftover.exists())
            self.assertTrue(any('bootout' in ' '.join(c) for c in calls))

    def test_retire_is_quiet_when_nothing_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            agents = Path(directory) / 'LaunchAgents'
            agents.mkdir()
            with mock.patch.object(asgctl, 'plist_path',
                                   lambda label: agents / (label + '.plist')):
                asgctl._retire_legacy_collector_service()  # must not raise


if __name__ == '__main__':
    unittest.main()

