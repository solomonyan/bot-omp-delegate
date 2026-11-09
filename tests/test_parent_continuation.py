"""Exercise Store + Worker against an offline rpc-ui subprocess peer.

Native OMP owns child-result delivery and the resulting turn in the same parent
session. Its prompt_result(sessionSettled=False) precedes resumed parent output
and session_settled. These tests verify the delegate's side of that documented
protocol, not the installed engine or a paid model-backed continuation.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from bot_omp_delegate.lifecycle import TERMINAL, Conflict, Store
from bot_omp_delegate.worker import Worker

RESUMED_TEXT = "Parent inspected child result 42; next step selected"

# test_resume is a fixture-only release barrier, never a production RPC command.
# No timers: the test observes the yielded parent before releasing completion.
PEER = r'''
import json, sys
inbox_path, mode = sys.argv[1:]
inbox = open(inbox_path, "a", buffering=1)
prompt_id = None
resumed_text = %r

def emit(frame):
    print(json.dumps(frame), flush=True)

def resume():
    if mode != "prompt-error":
        emit({"type": "subagent_event", "payload": {"id": "child-1", "event": {
            "type": "message_end", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "child result 42"}]}}}})
        emit({"type": "subagent_lifecycle", "payload": {"id": "child-1", "status": "completed"}})
        emit({"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "text", "text": resumed_text}]}})
    emit({"type": "session_settled"})
    if mode == "duplicate":
        emit({"type": "prompt_result", "id": prompt_id, "status": "completed", "sessionSettled": True})
        emit({"type": "session_settled"})

emit({"type": "ready", "supportedProtocolVersions": [1, 2]})
for line in sys.stdin:
    frame = json.loads(line)
    inbox.write(json.dumps(frame) + "\n")
    kind = frame.get("type")
    if kind == "prompt":
        prompt_id = frame["id"]
        emit({"type": "response", "id": prompt_id, "success": True, "data": {"accepted": True}})
        if mode == "immediate":
            emit({"type": "prompt_result", "id": prompt_id, "status": "completed", "sessionSettled": True})
            emit({"type": "session_settled"})
        elif mode == "prompt-error":
            emit({"type": "prompt_result", "id": prompt_id, "status": "error",
                  "error": {"message": "provider unavailable"}, "sessionSettled": False})
        else:
            emit({"type": "subagent_lifecycle", "payload": {"id": "child-1", "status": "started",
                  "sessionFile": "/offline/child.jsonl", "parentToolCallId": "tool-1"}})
            emit({"type": "prompt_result", "id": prompt_id, "status": "completed", "sessionSettled": False})
            if mode == "approval":
                emit({"type": "extension_ui_request", "id": "ui-1", "method": "confirm",
                      "title": "Approve task for this session", "message": "Run the offline task"})
    elif kind == "extension_ui_response":
        resume()
    else:
        if kind == "test_resume":
            resume()
        # Respond after all released frames: request() is also a reader barrier.
        emit({"type": "response", "id": frame["id"], "success": True, "data": {}})
''' % RESUMED_TEXT


class Sink:
    enabled = True

    def __init__(self):
        self.events = []

    def emit(self, payload):
        self.events.append(payload)


class ParentContinuationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name) / "project"
        self.cwd.mkdir()
        self.inbox = Path(temp.name) / "inbox.jsonl"
        self.sink = Sink()
        self.store = Store(Path(temp.name) / "state", self.sink)
        self.addCleanup(self.store.close)
        self.session = self.store.create({"name": "parent", "cwd": str(self.cwd)})
        self.sid = self.session["id"]

    def wait_for(self, predicate):
        with self.store.cv:
            self.assertTrue(self.store.cv.wait_for(predicate, timeout=10), "RPC peer did not reach expected state")

    def begin(self, mode):
        command = [sys.executable, "-u", "-c", PEER, str(self.inbox), mode]
        with patch.object(Worker, "_command", return_value=command):
            initial = self.store.turn(self.sid, "inspect child result, then choose the next step")
            job = self.store.jobs[initial["id"]]
            self.wait_for(lambda: "resultStatus" in job and self.sid not in self.store.startup_runs)
        return job, self.store.workers[self.sid]

    def prompt_count(self):
        frames = [json.loads(line) for line in self.inbox.read_text().splitlines()]
        return sum(frame["type"] == "prompt" for frame in frames)

    def test_child_result_keeps_same_parent_until_resumed_output_settles(self):
        job, worker = self.begin("child")
        other = self.store.create({"name": "other", "cwd": str(self.cwd)})
        with self.store.cv:
            self.assertEqual(job["status"], "running")
            self.assertEqual(self.store.session(self.sid)["activeJob"], job["id"])
            self.assertEqual(self.store.session(self.sid)["worker"]["state"], "busy")
            self.assertEqual(self.sink.events, [])
            with self.assertRaises(Conflict):
                self.store.turn(other["id"], "must not steal the worktree")
        worker.request("test_resume", timeout=10)
        self.wait_for(lambda: job["status"] in TERMINAL)
        self.assertEqual((job["status"], job["finalMessage"]), ("completed", RESUMED_TEXT))
        self.assertIs(self.store.workers[self.sid], worker)
        self.assertTrue(worker.alive)
        self.assertEqual(self.store.session(self.sid)["sessionDir"], self.session["sessionDir"])
        self.assertIsNone(self.store.session(self.sid)["activeJob"])
        self.assertEqual(self.prompt_count(), 1, "child completion must not submit a relay prompt")
        self.assertEqual([(event["event"], event["job_id"]) for event in self.sink.events],
                         [("run.done", job["id"])])
        messages = [event["message"] for event in self.store.events(job["id"], 0)["events"]
                    if event["type"] == "message_end"]
        self.assertEqual(messages[-1]["content"], [{"type": "text", "text": RESUMED_TEXT}])

    def test_duplicate_terminal_frames_do_not_repeat_parent_completion(self):
        job, worker = self.begin("duplicate")
        worker.request("test_resume", timeout=10)
        self.assertEqual((job["status"], job["finalMessage"]), ("completed", RESUMED_TEXT))
        self.assertEqual([event["event"] for event in self.sink.events], ["run.done"])
        self.assertEqual(self.prompt_count(), 1)

    def test_caller_approval_answer_allows_same_parent_to_continue(self):
        job, worker = self.begin("approval")
        self.wait_for(lambda: bool(job["pendingInput"]))
        self.assertEqual(job["status"], "waiting_input")
        self.assertEqual(self.sink.events[0]["input_id"], "ui-1")
        self.assertEqual([event["event"] for event in self.sink.events], ["run.needs_input"])
        # A real pending approval requires an explicit caller answer.
        self.store.control(job["id"], "answer", {"id": "ui-1", "confirmed": True})
        self.wait_for(lambda: job["status"] in TERMINAL)
        self.assertEqual(job["pendingInput"], [])
        self.assertEqual((job["status"], job["finalMessage"]), ("completed", RESUMED_TEXT))
        self.assertIs(self.store.workers[self.sid], worker)
        self.assertEqual(self.prompt_count(), 1)
        self.assertEqual([event["event"] for event in self.sink.events], ["run.needs_input", "run.done"])

    def test_yielded_error_remains_error_after_background_work_drains(self):
        job, worker = self.begin("prompt-error")
        self.assertEqual(job["status"], "running")
        worker.request("test_resume", timeout=10)
        self.assertEqual((job["status"], job["error"]), ("error", "provider unavailable"))
        self.assertEqual([event["event"] for event in self.sink.events], ["run.error"])

    def test_already_settled_prompt_does_not_finish_twice(self):
        job, worker = self.begin("immediate")
        worker.request("get_state", timeout=10)
        self.assertEqual(job["status"], "completed")
        self.assertEqual([event["event"] for event in self.sink.events], ["run.done"])


if __name__ == "__main__":
    unittest.main()
