import gzip
import json
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

    def test_completed_failure_text_still_emits_done_notification(self):
        job = self.run_job()
        self.event({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "FAIL: task completed with a reported failure"}]}})
        self.event({"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        self.assertEqual(self.names(), ["run.done"])
        self.assertEqual(self.sink.events[0]["summary"], "FAIL: task completed with a reported failure")

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

    def test_unanswered_session_approval_is_visible_and_suppresses_stalls(self):
        job = self.run_job()
        approval = {"type": "extension_ui_request", "method": "confirm", "id": "session-approval",
                    "title": "Approve task for this session"}
        self.event(approval)
        self.now = 10000
        self.store.monitor_tick()
        self.assertEqual(self.store.job(job["id"])["status"], "waiting_input")
        self.assertEqual(self.store.job(job["id"])["pendingInput"], [approval])
        self.assertEqual(self.names(), ["run.needs_input"])
        self.assertEqual(self.sink.events[0]["input"], approval)
        self.event({"type": "extension_ui_request", "method": "cancel", "targetId": "session-approval"})
        self.assertEqual(self.store.job(job["id"])["pendingInput"], [])
        self.now += 1800
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["run.needs_input", "run.stalled"])

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
    def test_stream_and_progress_are_dropped_while_final_results_and_questions_survive(self):
        job = self.run_job()
        started = {"type": "subagent_lifecycle", "payload": {"id": "child", "status": "started"}}
        self.event(started)
        for kind in ("tool_stream_update", "subagent_progress"):
            frame = {"type": kind, "text": "streaming progress"}
            self.event(frame)
            self.event({"type": "subagent_event", "payload": {"id": "child", "event": frame}})
            self.event({"type": "subagent_event", "payload": {"id": "child", "event": {
                "type": "subagent_event", "payload": {"id": "grandchild", "event": frame},
            }}})
        finals = [
            {"type": "tool_execution_end", "result": {"content": [{"type": "text", "text": "tool result"}]}},
            {"type": "subagent_event", "payload": {"id": "child", "event": {
                "type": "tool_execution_end", "result": {"content": [{"type": "text", "text": "child tool result"}]},
            }}},
            {"type": "subagent_event", "payload": {"id": "child", "event": {
                "type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "child final"}]},
            }}},
            {"type": "subagent_lifecycle", "payload": {"id": "child", "status": "completed"}},
            {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "parent final"}]}},
        ]
        for frame in finals:
            self.event(frame)
        question = {"type": "extension_ui_request", "id": "approval", "method": "confirm", "title": "Continue?"}
        self.event(question)
        self.assertEqual(self.store.job(job["id"])["pendingInput"], [question])
        self.assertEqual(self.sink.events[-1]["input"], question)
        cancel = {"type": "extension_ui_request", "method": "cancel", "targetId": "approval"}
        completed = {"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True}
        self.event(cancel)
        self.event(completed)
        expected = [dict(frame, seq=seq) for seq, frame in enumerate([started, *finals, question, cancel, completed])]
        path = self.store.root / "jobs" / f"{job['id']}.jsonl"
        self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], expected)
        self.assertEqual(self.store.events(job["id"], 0)["events"], expected)
        self.assertEqual(self.store.job(job["id"])["finalMessage"], "parent final")
        self.assertEqual(self.store.job(job["id"])["pendingInput"], [])
        self.assertEqual(self.store.job(job["id"])["status"], "completed")
        self.assertEqual(self.names(), ["run.needs_input", "run.done"])
        self.assertEqual(self.sink.events[-1]["message"], "parent final")

    def test_dropped_tool_streams_update_in_memory_stalls_but_progress_does_not_mask_silence(self):
        job = self.run_job()
        self.now = 1700
        self.event({"type": "tool_stream_update", "text": "tool output"})
        self.now = 3499
        progress = {"type": "subagent_progress", "text": "status only"}
        self.event(progress)
        self.event({"type": "subagent_event", "payload": {"id": "child", "event": progress}})
        self.store.monitor_tick()
        self.assertEqual(self.names(), [])
        self.now = 3500
        self.store.monitor_tick()
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["run.stalled"])
        self.now = 3600
        self.event({"type": "subagent_event", "payload": {"id": "child", "event": {
            "type": "tool_stream_update", "delta": "child tool output",
        }}})
        self.now = 5399
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["run.stalled"])
        self.now = 5400
        self.store.monitor_tick()
        self.assertEqual(self.names(), ["run.stalled", "run.stalled"])
        self.assertEqual(self.store.events(job["id"], 0), {"events": [], "next": 0})
        self.assertEqual(self.store.job(job["id"])["nextSeq"], 0)
        self.assertFalse((self.store.root / "jobs" / f"{job['id']}.jsonl").exists())

    def test_transient_delta_persistence_is_bounded_and_final_events_keep_sequences(self):
        import gzip
        import json

        job = self.run_job()
        self.now = 123
        delta = "x" * 256
        children = ("child-a", "child-b", "child-c")
        for index in range(200):
            content = [{"type": "text", "text": delta * (index + 1)}]
            message = {"role": "assistant", "content": content}
            update = {"type": "message_update", "message": message,
                      "assistantMessageEvent": {"type": "text_delta", "delta": delta,
                                                "partial": message}}
            self.event(update)
            self.event({"type": "tool_execution_update", "partialResult": {"content": content}})
            for child in children:
                self.event({"type": "subagent_event", "payload": {"id": child, "event": update}})
                self.event({"type": "subagent_event", "payload": {"id": child, "event": {
                    "type": "tool_execution_update", "partialResult": {"content": content},
                }}})
        final_text = delta * 200
        self.assertEqual(self.store.notifications.runs[job["id"]]["last_output"], 123)
        for child in children:
            self.event({"type": "subagent_event", "payload": {"id": child, "event": {
                "type": "message_end", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": final_text},
                ]},
            }}})
        self.event({"type": "tool_execution_end", "result": {"content": [{"type": "text", "text": final_text}]}})
        self.event({"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "text", "text": final_text},
        ]}})
        self.event({"type": "extension_ui_request", "id": "input-1", "method": "confirm", "title": "Continue?"})

        path = self.store.root / "jobs" / f"{job['id']}.jsonl"
        persisted_bytes = path.stat().st_size
        self.assertLess(persisted_bytes, len(final_text.encode("utf-8")) * 5 * 1.1)
        stored = self.store.events(job["id"], 0)
        self.assertEqual([event["seq"] for event in stored["events"]], list(range(len(stored["events"]))))
        self.assertEqual([event["type"] for event in stored["events"]], [
            "subagent_event", "subagent_event", "subagent_event", "tool_execution_end", "message_end", "extension_ui_request",
        ])
        for event in stored["events"][:3]:
            self.assertEqual(event["payload"]["event"]["message"]["content"][0]["text"], final_text)
        self.assertEqual(stored["events"][3]["result"]["content"][0]["text"], final_text)
        self.assertEqual(self.store.job(job["id"])["finalMessage"], final_text)
        self.assertEqual(self.store.job(job["id"])["pendingInput"][0]["id"], "input-1")
        self.assertEqual(self.store.notifications.runs[job["id"]]["last_output"], 123)

        compressed = path.with_suffix(".jsonl.gz")
        compressed.write_bytes(b"\x1f\x8b")
        self.assertEqual(self.store.events(job["id"], 0), stored)
        with path.open("rb") as source, gzip.open(compressed, "wb") as target:
            target.write(source.read())
        path.unlink()
        self.assertEqual(self.store.events(job["id"], 1)["events"], stored["events"][1:])

    def test_archive_and_resumed_plain_are_merged_deduplicated_and_paginated(self):
        job = self.run_job()
        directory = self.store.root / "jobs"
        plain = directory / f"{job['id']}.jsonl"
        archive = plain.with_suffix(".jsonl.gz")
        archived = [{"seq": seq, "type": "archived", "value": seq} for seq in range(120)]
        with gzip.open(archive, "wt", encoding="utf-8") as stream:
            for event in archived:
                stream.write(json.dumps(event) + "\n")
        resumed = [{"seq": seq, "type": "resumed", "value": seq} for seq in range(119, 140)]
        plain.write_text("".join(json.dumps(event) + "\n" for event in resumed))

        first = self.store.events(job["id"], 0)
        self.assertEqual([event["seq"] for event in first["events"]], list(range(100)))
        self.assertEqual(first["next"], 100)
        second = self.store.events(job["id"], first["next"])
        self.assertEqual([event["seq"] for event in second["events"]], list(range(100, 140)))
        self.assertEqual(second["events"][19]["type"], "resumed")

    def test_plain_log_survives_truncated_archive(self):
        job = self.run_job()
        plain = self.store.root / "jobs" / f"{job['id']}.jsonl"
        archive = plain.with_suffix(".jsonl.gz")
        plain.write_text(json.dumps({"seq": 0, "type": "committed"}) + "\n")
        archive.write_bytes(b"\x1f\x8btruncated")
        self.assertEqual(self.store.events(job["id"], 0)["events"], [{"seq": 0, "type": "committed"}])

    def test_archive_fallback_when_plain_disappears_during_open(self):
        job = self.run_job()
        directory = self.store.root / "jobs"
        plain = directory / f"{job['id']}.jsonl"
        archive = plain.with_suffix(".jsonl.gz")
        event = {"seq": 0, "type": "archived"}
        with gzip.open(archive, "wt", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        plain.write_text(json.dumps({"seq": 0, "type": "plain"}) + "\n")
        path_open = Path.open
        def unlink_during_plain_open(path, *args, **kwargs):
            if path == plain:
                plain.unlink()
            return path_open(path, *args, **kwargs)
        with patch.object(Path, "open", unlink_during_plain_open):
            self.assertEqual(self.store.events(job["id"], 0)["events"], [event])

    def test_plain_unlink_after_open_preserves_committed_events(self):
        job = self.run_job()
        plain = self.store.root / "jobs" / f"{job['id']}.jsonl"
        archive = plain.with_suffix(".jsonl.gz")
        event = {"seq": 0, "type": "committed"}
        archive.write_bytes(b"\x1f\x8b")
        plain.write_text(json.dumps(event) + "\n")
        path_open = Path.open
        def unlink_after_open(path, *args, **kwargs):
            stream = path_open(path, *args, **kwargs)
            if path == plain:
                plain.unlink()
            return stream
        with patch.object(Path, "open", unlink_after_open):
            self.assertEqual(self.store.events(job["id"], 0)["events"], [event])

    def test_partial_archive_finishes_when_plain_open_loses_cutover_race(self):
        job = self.run_job()
        plain = self.store.root / "jobs" / f"{job['id']}.jsonl"
        archive = plain.with_suffix(".jsonl.gz")
        event = {"seq": 0, "type": "committed"}
        archive.write_bytes(b"\x1f\x8b")
        plain.write_text(json.dumps(event) + "\n")
        path_open = Path.open
        def complete_cutover(path, *args, **kwargs):
            if path == plain and plain.exists():
                with gzip.open(archive, "wt", encoding="utf-8") as stream:
                    stream.write(json.dumps(event) + "\n")
                plain.unlink()
            return path_open(path, *args, **kwargs)
        with patch.object(Path, "open", complete_cutover):
            self.assertEqual(self.store.events(job["id"], 0)["events"], [event])


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
