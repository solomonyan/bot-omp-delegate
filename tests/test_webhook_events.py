from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot_omp_delegate.lifecycle import Conflict, Store


class Sink:
    enabled = True
    settings = {"stall_seconds": 1800, "idle_seconds": None}

    def __init__(self):
        self.events = []

    def emit(self, payload):
        self.events.append(payload)


class EventTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.now = 0
        self.sink = Sink()
        self.project = Path(temp.name) / "project"
        self.project.mkdir()
        self.store = Store(Path(temp.name) / "state", self.sink, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.lane = self.store.create_lane({"name": "coding", "cwd": str(self.project)})
        self.sid = self.lane["sessionId"]

    def run_job(self, label=None):
        with patch.object(self.store, "_run"):
            return self.store.lane_action(self.lane["id"], "run", {"message": "work", "label": label})

    def event(self, frame):
        self.store.event(self.sid, frame)

    def names(self):
        return [p["event"] for p in self.sink.events]

    def test_done_only_after_settled_with_lane_label_and_timestamp(self):
        job = self.run_job("OPS-42")
        self.event({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "Implemented the requested fix"}]}})
        self.event({"type": "prompt_result", "id": job["id"], "status": "completed"})
        self.assertEqual(self.names(), [])
        self.event({"type": "session_settled"})
        self.assertEqual(self.names(), ["run.done"])
        payload = self.sink.events[0]
        self.assertEqual((payload["job_id"], payload["lane_id"], payload["lane"], payload["label"]), (job["id"], self.lane["id"], "coding", "OPS-42"))
        self.assertEqual((payload["status"], payload["summary"], payload["cwd"]), ("completed", "Implemented the requested fix", str(self.project.resolve())))
        self.assertIsNotNone(datetime.fromisoformat(payload["ts"]).utcoffset())
        self.event({"type": "session_settled"})
        self.assertEqual(self.names(), ["run.done"])

    def test_error_aborted_and_interrupted_are_not_success(self):
        for status in ("error", "aborted", "interrupted"):
            with self.subTest(status=status):
                job = self.run_job()
                self.event({"type": "prompt_result", "id": job["id"], "status": status, "error": {"message": "provider or cancellation reason"}, "sessionSettled": True})
                payload = self.sink.events[-1]
                self.assertEqual((payload["event"], payload["status"], payload["summary"]), ("run.error", status, "provider or cancellation reason"))
        self.assertEqual(self.names(), ["run.error"] * 3)

    def test_each_pending_question_emits_once_and_waiting_suppresses_stalls(self):
        self.run_job()
        self.event({"type": "extension_ui_request", "method": "confirm", "id": "first", "title": "Approve tool execution?"})
        self.store.monitor_tick()
        self.now = 10000
        self.store.monitor_tick()
        self.event({"type": "extension_ui_request", "method": "input", "id": "second", "title": "Which ticket?"})
        self.assertEqual(self.names(), ["run.needs_input", "run.needs_input"])
        self.assertEqual([p["summary"] for p in self.sink.events], ["Approve tool execution?", "Which ticket?"])
        self.event({"type": "extension_ui_request", "method": "cancel", "targetId": "first"})
        self.event({"type": "extension_ui_request", "method": "cancel", "targetId": "second"})
        self.store.monitor_tick()
        self.now += 1799
        self.store.monitor_tick()
        self.assertNotIn("run.stalled", self.names())
        self.now += 1
        self.store.monitor_tick()
        self.assertEqual(self.names()[-1], "run.stalled")

    def test_stall_threshold_deduplication_and_actual_output_rearms(self):
        job = self.run_job()
        self.now = 1799
        self.store.monitor_tick()
        self.assertEqual(self.names(), [])
        self.event({"type": "response", "id": job["id"], "success": True})
        self.event({"type": "heartbeat"})
        self.now = 1800
        self.store.monitor_tick()
        self.now = 9000
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["run.stalled"])
        self.event({"type": "message_update", "assistantMessageEvent": {"delta": "new progress"}})
        self.now += 1800
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["run.stalled", "run.stalled"])
        self.assertEqual(self.store.job(job["id"])["status"], "running")

    def test_subagent_and_tool_output_reset_the_stall_clock(self):
        self.run_job()
        for frame in ({"type": "tool_stream_update", "text": "tool progress"}, {"type": "subagent_event", "payload": {"event": {"type": "message_update", "assistantMessageEvent": {"delta": "child progress"}}}}):
            self.now += 1700
            self.event(frame)
            self.now += 100
            self.store.monitor_tick()
        self.assertEqual(self.names(), [])
        self.now += 1700
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["run.stalled"])

    def test_deadline_is_once_per_live_job_and_never_cancels(self):
        job = self.run_job("OPS-9")
        self.store.deadline(job["id"])
        self.store.deadline(job["id"])
        self.assertEqual(self.names(), ["run.deadline"])
        self.assertEqual(self.sink.events[0]["label"], "OPS-9")
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        self.assertNotIn("cancelRequested", self.store.job(job["id"]))
        self.event({"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        self.store.deadline(job["id"])
        self.assertEqual(self.names(), ["run.deadline", "run.done"])

    def test_idle_is_optional_once_per_episode_and_respects_transitions(self):
        self.store.monitor_tick()
        self.now = 10000
        self.store.monitor_tick()
        self.assertEqual(self.names(), [])
        self.sink.settings = {"stall_seconds": 1800, "idle_seconds": 60}
        self.store.monitor_tick()
        self.now += 60
        self.store.monitor_tick()
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["lane.idle"])
        self.store.lane_operations.add(self.lane["id"])
        self.now += 600
        self.store.monitor_tick()
        self.store.lane_operations.clear()
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["lane.idle"])
        self.now += 60
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["lane.idle", "lane.idle"])
        job = self.run_job()
        self.store.monitor_tick()
        self.event({"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        self.now += 60
        self.store.monitor_tick()
        self.assertEqual(self.names()[-2:], ["run.done", "lane.idle"])

    def test_broken_delivery_does_not_change_job_outcome(self):
        job = self.run_job()
        with patch.object(self.sink, "emit", side_effect=RuntimeError("synthetic-secret-must-not-be-logged")):
            self.event({"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        self.assertEqual(self.store.job(job["id"])["status"], "completed")
        self.assertEqual(self.store.notifications.failures, 1)

    def test_disabled_notifications_have_no_monitor_or_events(self):
        self.sink.enabled = False
        self.store.start_monitor()
        job = self.run_job()
        self.now = 10000
        self.store.monitor_tick()
        self.event({"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        self.assertIsNone(self.store._monitor_thread)
        self.assertEqual(self.sink.events, [])

    def test_idle_shutdown_refuses_active_and_transitioning_sessions(self):
        job = self.run_job()
        with self.assertRaises(Conflict):
            self.store.request_shutdown(True)
        self.assertFalse(self.store.closing)
        self.event({"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        self.store.lane_operations.add(self.lane["id"])
        with self.assertRaises(Conflict):
            self.store.request_shutdown(True)
        self.store.lane_operations.clear()
        self.store.request_shutdown(True)
        with self.assertRaises(Conflict):
            self.run_job()

    def test_output_while_subscribers_disabled_prevents_false_stall_after_reload(self):
        self.run_job()
        self.sink.enabled = False
        self.now = 1700
        self.event({"type": "tool_stream_update", "text": "Actual progress while notifications are disabled"})
        self.sink.enabled = True
        self.now = 1800
        self.store.monitor_tick()
        self.assertEqual(self.names(), [])
        self.now = 3500
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["run.stalled"])

    def test_question_answered_while_disabled_restarts_inactivity_interval(self):
        self.run_job()
        self.sink.enabled = False
        self.event({"type": "extension_ui_request", "method": "confirm", "id": "approval"})
        self.now = 10000
        self.event({"type": "extension_ui_request", "method": "cancel", "targetId": "approval"})
        self.sink.enabled = True
        self.store.monitor_tick()
        self.assertEqual(self.names(), [])
        self.now += 1800
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["run.stalled"])


    def test_invalid_label_rejected_before_any_job_is_created(self):
        for label in ("", "a\nb", "x" * 201, 3):
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.run_job(label)
        self.assertEqual(self.store.jobs, {})


if __name__ == "__main__":
    unittest.main()
