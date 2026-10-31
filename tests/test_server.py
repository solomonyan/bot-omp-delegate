import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot_omp_delegate.lifecycle import Conflict, Store


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.store = Store(self.root / "state")
        self.session = self.store.create({"name": "test", "cwd": str(self.cwd)})

    def begin(self, sid=None):
        with patch.object(self.store, "_run"):
            return self.store.turn(sid or self.session["id"], "Do work")

    def event(self, frame):
        self.store.event(self.session["id"], frame)

    def test_yield_retains_directory_ownership_until_background_work_settles(self):
        job = self.begin()
        other = self.store.create({"name": "other", "cwd": str(self.cwd)})
        self.event({"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": False})
        with self.assertRaises(Conflict):
            self.begin(other["id"])
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        self.event({"type": "session_settled"})
        self.assertEqual(self.store.job(job["id"])["status"], "completed")
        self.assertIsNone(self.store.session(self.session["id"])["activeJob"])
        self.begin(other["id"])

    def test_startup_settled_event_does_not_complete_unsubmitted_prompt(self):
        job = self.begin()
        self.event({"type": "session_settled"})
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        self.event({"type": "delegate_worker_exit", "error": "worker crashed"})
        self.assertEqual(self.store.job(job["id"])["status"], "error")
        self.assertEqual(self.store.job(job["id"])["error"], "worker crashed")

    def test_ui_cancel_clears_pending_input_without_auto_approval(self):
        job = self.begin()
        self.event({"type": "extension_ui_request", "method": "confirm", "id": "approval", "title": "Allow?"})
        self.assertEqual(self.store.job(job["id"])["status"], "waiting_input")
        self.assertEqual(self.store.job(job["id"])["pendingInput"][0]["id"], "approval")
        self.event({"type": "extension_ui_request", "method": "cancel", "targetId": "approval"})
        self.assertEqual(self.store.job(job["id"])["pendingInput"], [])
        self.assertEqual(self.store.job(job["id"])["status"], "running")

    def test_restart_marks_unsettled_job_interrupted_and_retains_transcript(self):
        job = self.begin()
        self.event({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "Partial analysis"}]}})
        restarted = Store(self.root / "state")
        saved = restarted.job(job["id"])
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["finalMessage"], "Partial analysis")
        self.assertIsNone(restarted.session(self.session["id"])["activeJob"])
        events = restarted.events(job["id"], 0)
        self.assertEqual(events["events"][0]["message"]["content"][0]["text"], "Partial analysis")

    def test_provider_error_remains_error_after_settling(self):
        job = self.begin()
        self.event({"type": "prompt_result", "id": job["id"], "status": "error", "error": {"message": "quota exhausted"}, "sessionSettled": False})
        self.event({"type": "session_settled"})
        self.assertEqual(self.store.job(job["id"])["status"], "error")
        self.assertEqual(self.store.job(job["id"])["error"], "quota exhausted")


if __name__ == "__main__":
    unittest.main()
