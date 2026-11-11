"""Grace-delayed run.needs_input for tool approvals that scoped approvers clear."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot_omp_delegate.lifecycle import Store
from bot_omp_delegate.notifications import is_tool_approval
from bot_omp_delegate.webhooks import ConfigError, WebhookDispatcher, needs_input_grace


APPROVAL = {"type": "extension_ui_request", "method": "select", "id": "a1",
            "title": "Allow tool: browser", "options": ["Approve", "Deny"]}


class Sink:
    enabled = True

    def __init__(self, grace, roots):
        self.events = []
        self.settings = {"stall_seconds": 1800, "idle_seconds": None,
                         "needs_input_grace_seconds": grace, "needs_input_grace_roots": roots}

    def emit(self, payload):
        self.events.append(payload)


class GraceTests(unittest.TestCase):
    def setUp(self, grace=30):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.design = base / "design"
        (self.design / "checkout").mkdir(parents=True)
        self.other = base / "repo"
        self.other.mkdir()
        self.now = 0
        self.sink = Sink(grace, (str(self.design),))
        self.store = Store(base / "state", self.sink, clock=lambda: self.now)
        self.addCleanup(self.store.close)

    def start(self, cwd):
        lane = self.store.create_lane({"name": f"l{len(self.store.lanes)}", "cwd": str(cwd)})
        with patch.object(self.store, "_run"):
            job = self.store.lane_action(lane["id"], "run", {"message": "work"})
        return lane["sessionId"], job

    def ask(self, sid, **frame):
        self.store.event(sid, {**APPROVAL, **frame})

    def at(self, seconds):
        self.now = seconds
        self.store.monitor_tick()

    def needs(self):
        return [p["input_id"] for p in self.sink.events if p["event"] == "run.needs_input"]

    def names(self):
        return [p["event"] for p in self.sink.events]

    def test_approval_answered_within_grace_is_never_forwarded(self):
        sid, job = self.start(self.design / "checkout")
        self.ask(sid)
        self.at(5)
        self.assertEqual(self.needs(), [])
        live = self.store.jobs[job["id"]]
        worker = type("Worker", (), {"respond_ui": lambda self, data: None})()
        with patch.object(self.store, "_job_worker", return_value=(live, self.store.session(sid), worker)):
            self.store.control(job["id"], "answer", {"id": "a1", "value": "Approve"})  # the approver's answer path
        self.at(20)
        self.at(31)
        self.at(1000)
        self.assertEqual(self.needs(), [])

    def test_cancelled_within_grace_is_dropped(self):
        sid, _ = self.start(self.design / "checkout")
        self.ask(sid)
        self.store.event(sid, {"type": "extension_ui_request", "method": "cancel", "id": "c", "targetId": "a1"})
        self.at(60)
        self.assertEqual(self.needs(), [])

    def test_still_pending_after_grace_forwards_once_unchanged(self):
        sid, job = self.start(self.design / "checkout")
        self.ask(sid)
        self.at(29.9)
        self.assertEqual(self.needs(), [])
        self.at(30)
        self.at(31)
        self.at(500)
        self.ask(sid)  # a replayed frame for the same id must not re-forward
        self.at(600)
        self.assertEqual(self.needs(), ["a1"])
        payload = self.sink.events[0]
        self.assertEqual(payload["event_id"], f"{job['id']}:run.needs_input:a1")
        self.assertEqual(payload["input"]["title"], "Allow tool: browser")

    def test_real_question_in_design_root_forwards_immediately(self):
        sid, _ = self.start(self.design / "checkout")
        self.store.event(sid, {"type": "extension_ui_request", "method": "select", "id": "q1",
                               "title": "Which layout should C3 use?", "options": ["Dense", "Roomy"]})
        self.store.event(sid, {"type": "extension_ui_request", "method": "input", "id": "q2", "title": "Ticket?"})
        self.assertEqual(self.needs(), ["q1", "q2"])

    def test_approval_outside_roots_forwards_immediately(self):
        sid, _ = self.start(self.other)
        self.ask(sid)
        self.assertEqual(self.needs(), ["a1"])

    def test_root_prefix_lookalike_is_outside(self):
        lookalike = self.design.parent / (self.design.name + "-evil")
        lookalike.mkdir()
        sid, _ = self.start(lookalike)
        self.ask(sid)
        self.assertEqual(self.needs(), ["a1"])

    def test_done_and_error_are_immediate_and_drop_held_prompt(self):
        sid, job = self.start(self.design / "checkout")
        self.ask(sid)
        self.store.event(sid, {"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        self.assertEqual(self.names(), ["run.done"])
        self.at(100)
        self.assertEqual(self.names(), ["run.done"])
        sid2, job2 = self.start(self.design / "checkout")
        self.ask(sid2, id="a2")
        self.store.event(sid2, {"type": "prompt_result", "id": job2["id"], "status": "error",
                                "error": {"message": "boom"}, "sessionSettled": True})
        self.assertEqual(self.names(), ["run.done", "run.error"])

    def test_grace_zero_is_todays_behaviour(self):
        self.sink.settings["needs_input_grace_seconds"] = 0
        sid, _ = self.start(self.design / "checkout")
        self.ask(sid)
        self.assertEqual(self.needs(), ["a1"])

    def test_shutdown_forwards_still_pending_held_prompt(self):
        sid, _ = self.start(self.design / "checkout")
        self.ask(sid)
        self.store.close()
        self.assertEqual(self.needs(), ["a1"])
        self.assertEqual(self.names().count("run.needs_input"), 1)

    def test_hold_does_not_block_other_jobs(self):
        sid, _ = self.start(self.design / "checkout")
        self.ask(sid)
        sid2, _ = self.start(self.other)
        self.ask(sid2, id="b1")
        self.assertEqual(self.needs(), ["b1"])
        self.at(30)
        self.assertEqual(self.needs(), ["b1", "a1"])


class ClassifierTests(unittest.TestCase):
    def test_tool_approval_shape(self):
        self.assertTrue(is_tool_approval(APPROVAL))
        self.assertTrue(is_tool_approval({**APPROVAL, "title": "Allow tool: eval\nCode: x",
                                          "options": ["Approve", "Approve eval for this session", "Deny"]}))
        self.assertFalse(is_tool_approval({**APPROVAL, "method": "input"}))
        self.assertFalse(is_tool_approval({**APPROVAL, "title": "Pick one", "options": ["A", "B"]}))
        self.assertFalse(is_tool_approval({**APPROVAL, "options": ["Yes", "No"]}))


class ConfigTests(unittest.TestCase):
    def test_defaults_env_override_and_kill_switch(self):
        seconds, roots = needs_input_grace({}, environ={})
        self.assertEqual(seconds, 30)
        self.assertEqual(roots, (os.path.realpath(os.path.expanduser("~/lanes/omp-runs/design/launch-2026-11-11/")),))
        self.assertEqual(needs_input_grace({"needs_input_grace_seconds": 5}, environ={})[0], 5)
        env = {"OMP_DELEGATE_NEEDS_INPUT_GRACE_SECONDS": "0", "OMP_DELEGATE_NEEDS_INPUT_GRACE_ROOTS": "/a" + os.pathsep + "/b"}
        self.assertEqual(needs_input_grace({}, environ=env), (0, (os.path.realpath("/a"), os.path.realpath("/b"))))
        self.assertEqual(needs_input_grace({}, environ={"OMP_DELEGATE_NEEDS_INPUT_GRACE_SECONDS": "nope"})[0], 30)

    def test_config_file_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            root.mkdir(mode=0o700)
            path = root / "webhooks.json"
            for value, ok in ((0, True), (45, True), (-1, False), ("30", False)):
                path.write_text(json.dumps({"version": 1, "subscribers": [], "needs_input_grace_seconds": value}))
                path.chmod(0o600)
                dispatcher = WebhookDispatcher(root)
                self.assertEqual("config_error" not in dispatcher.public_config(), ok, value)
                dispatcher.close()
            path.write_text(json.dumps({"version": 1, "subscribers": [], "needs_input_grace_roots": "/x"}))
            path.chmod(0o600)
            with self.assertRaises(ConfigError):
                WebhookDispatcher(root).reload()


if __name__ == "__main__":
    unittest.main()
