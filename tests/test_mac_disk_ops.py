import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("mac_disk_ops", Path(__file__).resolve().parents[1] / "scripts/mac-disk/mac_disk_ops.py")
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


class DiskOpsTests(unittest.TestCase):
    def test_thresholds_latch_independently_until_above_sixty(self):
        state = {"sent": []}
        self.assertEqual(ops.alert_thresholds(49, state), [50])
        state["sent"].append(50)
        self.assertEqual(ops.alert_thresholds(24, state), [25])
        state["sent"].append(25)
        state["notification25"] = True
        for free in (24, 26, 51, 60):
            self.assertEqual(ops.alert_thresholds(free, state), [])
        self.assertEqual(ops.alert_thresholds(61, state), [])
        self.assertNotIn("notification25", state)
        self.assertEqual(ops.alert_thresholds(24, state), [50, 25])

    def test_weekly_fails_closed_for_active_or_corrupt_job(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            job = root / "one.json"
            with patch.object(ops, "DATA", root), patch.object(ops, "JOBS", root), patch.object(ops.subprocess, "run") as run:
                for text in ('{"status":"running"}', '{"status":"waiting_input"}', '{"status":"unknown"}', 'broken', '{}'):
                    job.write_text(text)
                    ops.weekly()
                    run.assert_not_called()

    def test_failed_delivery_retries_without_duplicate_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(ops, "DATA", root), patch.object(ops, "daily_sizes"), patch.object(ops.subprocess, "run") as run:
                run.return_value.returncode = 1
                ops.watch(49)
                self.assertEqual(ops.load(root / "watch-state.json", {})["sent"], [])
                run.return_value.returncode = 0
                ops.watch(49)
                self.assertEqual(ops.load(root / "watch-state.json", {})["sent"], [50])
                run.reset_mock()
                ops.watch(49)
                run.assert_not_called()

    def test_weekly_rechecks_idle_between_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(ops, "DATA", Path(folder)), patch.object(ops, "jobs_idle", side_effect=[True, True, False]), patch.object(ops.subprocess, "run") as run:
                run.return_value.returncode = 0
                ops.weekly()
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0], ["brew", "cleanup", "-s"])
                self.assertFalse((Path(folder) / "weekly-state.json").exists())
