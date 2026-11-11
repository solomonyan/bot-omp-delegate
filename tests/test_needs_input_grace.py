"""run.needs_input is held for a grace period; browser prompts in design sessions are pre-approved."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot_omp_delegate.lifecycle import Store
from bot_omp_delegate.notifications import is_tool_approval, one_time_approval
from bot_omp_delegate.webhooks import ConfigError, WebhookDispatcher, needs_input_grace, tool_preapprovals


APPROVAL = {"type": "extension_ui_request", "method": "select", "id": "a1",
            "title": "Allow tool: bash", "options": ["Approve", "Deny"]}
BROWSER = {**APPROVAL, "id": "b1", "title": "Allow tool: browser"}


class Sink:
    enabled = True

    def __init__(self, grace, preapprovals):
        self.events, self.filtered = [], []
        self.settings = {"stall_seconds": 1800, "idle_seconds": None,
                         "needs_input_grace_seconds": grace, "tool_preapprovals": preapprovals}

    def emit(self, payload, coordinator_subscriber_id=None):
        self.events.append(payload)

    def record_filtered(self, payload, reason):
        self.filtered.append((payload["input_id"], reason))


class Worker:
    alive, pid = False, None

    def __init__(self):
        self.answers = []

    def respond_ui(self, data):
        self.answers.append(data)

    def close(self):
        pass


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
        self.sink = Sink(grace, ((str(self.design), frozenset({"browser"})),))
        self.store = Store(base / "state", self.sink, clock=lambda: self.now)
        self.addCleanup(self.store.close)

    def start(self, cwd):
        lane = self.store.create_lane({"name": f"l{len(self.store.lanes)}", "cwd": str(cwd)})
        with patch.object(self.store, "_run"):
            job = self.store.lane_action(lane["id"], "run", {"message": "work"})
        worker = self.store.workers[lane["sessionId"]] = Worker()
        return lane["sessionId"], job, worker

    def ask(self, sid, **frame):
        self.store.event(sid, {**APPROVAL, **frame})

    def at(self, seconds):
        self.now = seconds
        self.store.monitor_tick()

    def answer(self, sid, job, ui_id):
        live = self.store.jobs[job["id"]]
        with patch.object(self.store, "_job_worker", return_value=(live, self.store.session(sid), Worker())):
            self.store.control(job["id"], "answer", {"id": ui_id, "value": "Approve"})

    def needs(self):
        return [p["input_id"] for p in self.sink.events if p["event"] == "run.needs_input"]

    def names(self):
        return [p["event"] for p in self.sink.events]

    def test_approval_answered_within_grace_is_logged_not_forwarded(self):
        sid, job, _ = self.start(self.other)
        self.ask(sid)
        self.at(5)
        self.answer(sid, job, "a1")  # the scoped approver's answer path
        self.ask(sid, id="a2")
        self.answer(sid, job, "a2")
        self.store.event(sid, {"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        self.at(20)
        self.at(31)
        self.at(1000)
        self.assertEqual(self.needs(), [])
        self.assertEqual(self.sink.filtered, [("a1", "resolved_within_grace"), ("a2", "resolved_within_grace")])

    def test_cancelled_within_grace_is_dropped(self):
        sid, _, _ = self.start(self.design / "checkout")
        self.ask(sid)
        self.store.event(sid, {"type": "extension_ui_request", "method": "cancel", "id": "c", "targetId": "a1"})
        self.at(60)
        self.assertEqual(self.needs(), [])

    def test_still_pending_after_grace_forwards_once_unchanged(self):
        sid, job, _ = self.start(self.design / "checkout")
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
        self.assertEqual(payload["input"]["title"], "Allow tool: bash")

    def test_real_questions_anywhere_wait_out_the_grace_then_forward(self):
        sid, _, _ = self.start(self.other)
        self.store.event(sid, {"type": "extension_ui_request", "method": "select", "id": "q1",
                               "title": "Which layout should C3 use?", "options": ["Dense", "Roomy"]})
        self.store.event(sid, {"type": "extension_ui_request", "method": "input", "id": "q2", "title": "Ticket?"})
        self.at(29)
        self.assertEqual(self.needs(), [])
        self.at(30)
        self.assertEqual(self.needs(), ["q1", "q2"])

    def test_browser_prompt_in_design_session_is_answered_by_the_daemon(self):
        sid, job, worker = self.start(self.design / "checkout")
        self.store.event(sid, BROWSER)
        self.assertEqual(worker.answers, [{"id": "b1", "value": "Approve"}])
        self.assertEqual(self.store.job(job["id"])["pendingInput"], [])
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        self.at(1000)
        self.assertEqual(self.needs(), [])
        self.assertEqual(self.sink.filtered, [("b1", "preapproved")])

    def test_preapproval_never_widens_to_other_tools_sessions_or_grants(self):
        sid, job, worker = self.start(self.design / "checkout")
        self.ask(sid)  # bash in a design session
        self.store.event(sid, {**BROWSER, "id": "b2", "title": "Allow tool: browser_extra"})
        self.store.event(sid, {**BROWSER, "id": "b3", "options": ["Approve browser for this session", "Deny"]})
        sid2, _, worker2 = self.start(self.other)
        self.store.event(sid2, {**BROWSER, "id": "b4"})  # browser outside the design root
        lookalike = self.design.parent / (self.design.name + "-evil")
        lookalike.mkdir()
        sid3, _, worker3 = self.start(lookalike)
        self.store.event(sid3, {**BROWSER, "id": "b5"})
        self.assertEqual((worker.answers, worker2.answers, worker3.answers), ([], [], []))
        self.at(30)
        self.assertEqual(sorted(self.needs()), ["a1", "b2", "b3", "b4", "b5"])

    def test_unanswerable_preapproval_stays_pending_for_a_person(self):
        sid, job, worker = self.start(self.design / "checkout")
        worker.respond_ui = lambda data: (_ for _ in ()).throw(BrokenPipeError())
        self.store.event(sid, BROWSER)
        self.assertEqual([p["id"] for p in self.store.job(job["id"])["pendingInput"]], ["b1"])
        self.at(30)
        self.assertEqual(self.needs(), ["b1"])

    def test_done_and_error_are_immediate_and_drop_held_prompt(self):
        sid, job, _ = self.start(self.design / "checkout")
        self.ask(sid)
        self.store.event(sid, {"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        self.assertEqual(self.names(), ["run.done"])
        self.at(100)
        self.assertEqual(self.names(), ["run.done"])
        sid2, job2, _ = self.start(self.design / "checkout")
        self.ask(sid2, id="a2")
        self.store.event(sid2, {"type": "prompt_result", "id": job2["id"], "status": "error",
                                "error": {"message": "boom"}, "sessionSettled": True})
        self.assertEqual(self.names(), ["run.done", "run.error"])
        self.assertEqual(self.sink.filtered, [("a1", "resolved_within_grace"), ("a2", "resolved_within_grace")])

    def test_grace_zero_forwards_immediately(self):
        self.sink.settings["needs_input_grace_seconds"] = 0
        sid, _, _ = self.start(self.design / "checkout")
        self.ask(sid)
        self.assertEqual(self.needs(), ["a1"])

    def test_shutdown_forwards_still_pending_held_prompt(self):
        sid, _, _ = self.start(self.design / "checkout")
        self.ask(sid)
        self.store.close()
        self.assertEqual(self.needs(), ["a1"])
        self.assertEqual(self.names().count("run.needs_input"), 1)

    def test_hold_does_not_block_other_jobs(self):
        sid, _, _ = self.start(self.design / "checkout")
        self.ask(sid)
        self.at(10)
        sid2, _, _ = self.start(self.other)
        self.ask(sid2, id="b1")
        self.at(30)
        self.assertEqual(self.needs(), ["a1"])
        self.at(40)
        self.assertEqual(self.needs(), ["a1", "b1"])


class ClassifierTests(unittest.TestCase):
    def test_tool_approval_shape(self):
        self.assertTrue(is_tool_approval(APPROVAL))
        self.assertTrue(is_tool_approval({**APPROVAL, "title": "Allow tool: eval\nCode: x",
                                          "options": ["Approve", "Approve eval for this session", "Deny"]}))
        self.assertFalse(is_tool_approval({**APPROVAL, "method": "input"}))
        self.assertFalse(is_tool_approval({**APPROVAL, "title": "Pick one", "options": ["A", "B"]}))
        self.assertFalse(is_tool_approval({**APPROVAL, "options": ["Yes", "No"]}))

    def test_one_time_approval_never_picks_a_wider_grant(self):
        self.assertEqual(one_time_approval({"options": ["Approve browser for this session", "Approve", "Deny"]}), "Approve")
        self.assertIsNone(one_time_approval({"options": ["Always allow", "Deny"]}))


class ConfigTests(unittest.TestCase):
    def test_defaults_env_override_and_kill_switch(self):
        self.assertEqual(needs_input_grace({}, environ={}), 30)
        self.assertEqual(needs_input_grace({"needs_input_grace_seconds": 5}, environ={}), 5)
        self.assertEqual(needs_input_grace({}, environ={"OMP_DELEGATE_NEEDS_INPUT_GRACE_SECONDS": "0"}), 0)
        self.assertEqual(needs_input_grace({}, environ={"OMP_DELEGATE_NEEDS_INPUT_GRACE_SECONDS": "nope"}), 30)

    def test_default_preapproval_is_browser_in_design_root_only(self):
        (root, tools), = tool_preapprovals({})
        self.assertEqual(root, str(Path("~/lanes/omp-runs/design").expanduser().resolve()))
        self.assertEqual(tools, frozenset({"browser"}))
        self.assertEqual(tool_preapprovals({"tool_preapprovals": []}), ())

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
            path.write_text(json.dumps({"version": 1, "subscribers": [], "needs_input_grace_roots": ["/x"]}))
            path.chmod(0o600)
            with self.assertRaises(ConfigError):
                WebhookDispatcher(root).reload()


if __name__ == "__main__":
    unittest.main()
