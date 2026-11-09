import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_omp_delegate.lifecycle import Store
from bot_omp_delegate.webhooks import WebhookDispatcher, add_subscriber


class CoordinatorWakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.root.mkdir(mode=0o700)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.now = 0
        for subscriber_id, mode in (("owner", "conversation"), ("other", "conversation"),
                                    ("monitor", "monitor")):
            add_subscriber(self.root, {
                "id": subscriber_id, "mode": mode,
                "url": "https://example.invalid/hook",
                # Explicit ownership must not depend on these observer filters.
                "events": ["daemon.up"] if mode == "conversation" else [],
                "lanes": ["different-lane"] if mode == "conversation" else [],
            })
        self.dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(self.dispatcher.close)
        self.enqueued = []

        def capture(envelope, subscribers, batch=None):
            self.enqueued.append((json.loads(envelope.body()), [item["id"] for item in subscribers]))

        self.submit_patch = patch.object(self.dispatcher, "_submit", side_effect=capture)
        self.submit_patch.start()
        self.addCleanup(self.submit_patch.stop)
        self.store = Store(self.root, self.dispatcher, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.lane = self.store.create_lane({"name": "coding", "cwd": str(self.project)})
        self.sid = self.lane["sessionId"]

    def run_job(self, owner="owner"):
        with patch.object(self.store, "_run"):
            return self.store.lane_action(self.lane["id"], "run", {
                "message": "work", "coordinator_subscriber_id": owner,
            })

    def finish(self, job, status="completed", **extra):
        self.store.event(self.sid, {
            "type": "prompt_result", "id": job["id"], "status": status,
            "sessionSettled": True, **extra,
        })

    def selected(self, event):
        return [(payload, ids) for payload, ids in self.enqueued if payload["event"] == event]

    def test_final_response_reaches_only_owner_after_background_work_settles(self):
        job = self.run_job()
        text = "Verified full response. " * 40
        self.store.event(self.sid, {"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}],
        }})
        self.store.event(self.sid, {"type": "prompt_result", "id": job["id"],
                                    "status": "completed", "sessionSettled": False})
        self.assertEqual(self.selected("run.done"), [])
        self.store.event(self.sid, {"type": "session_settled"})
        self.finish(job)
        self.store.event(self.sid, {"type": "session_settled"})
        done = self.selected("run.done")
        self.assertEqual(len(done), 1)
        payload, ids = done[0]
        self.assertEqual(ids, ["owner"])
        self.assertEqual(payload["message"], text)
        self.assertEqual(payload["summary"], text[:300])
        self.assertEqual((payload["channel"], payload["session_id"], payload["event_id"]),
                         ("conversation", self.sid, f"{job['id']}:run.done"))

    def test_ownerless_result_is_readable_without_broadcast(self):
        job = self.run_job(None)
        self.finish(job)
        self.assertEqual(self.store.job(job["id"])["status"], "completed")
        self.assertEqual(self.selected("run.done")[0][1], [])

    def test_result_content_does_not_route_to_another_coordinator(self):
        for text in ("Verdict: FAIL", "BLOCKED", "Verdict: PASS", "READY.md withheld"):
            with self.subTest(text=text):
                job = self.run_job()
                self.store.event(self.sid, {"type": "message_end", "message": {
                    "role": "assistant", "content": [{"type": "text", "text": text}],
                }})
                self.finish(job)
                self.assertEqual(self.selected("run.done")[-1][1], ["owner"])

    def test_pending_dialog_is_actionable_and_not_hidden_by_its_title(self):
        job = self.run_job()
        request = {"type": "extension_ui_request", "method": "select", "id": "approval",
                   "title": "Allow tool: bash", "options": ["Approve", "Approve bash for this session", "Deny"],
                   "optionDetails": [{"description": "Run the command"}]}
        self.store.event(self.sid, request)
        self.store.event(self.sid, request)
        self.now = 10000
        self.store.monitor_tick()
        self.assertEqual(self.store.job(job["id"])["pendingInput"], [request])
        self.assertEqual(self.store.job(job["id"])["status"], "waiting_input")
        decisions = self.selected("run.needs_input")
        self.assertEqual(len(decisions), 1)
        payload, ids = decisions[0]
        self.assertEqual(ids, ["owner"])
        self.assertEqual(payload["input"], request)
        self.assertEqual(payload["input_id"], "approval")
        self.assertEqual(payload["event_id"], f"{job['id']}:run.needs_input:approval")
        self.assertEqual(self.selected("run.stalled"), [])

    def test_each_genuine_question_keeps_choices_and_its_own_identity(self):
        job = self.run_job()
        for ui_id in ("first", "second"):
            request = {"type": "extension_ui_request", "method": "ask", "id": ui_id,
                       "questions": [{"id": "destination", "question": "Which environment?",
                                      "options": [{"label": "Staging", "description": "No production writes"},
                                                  {"label": "Production"}], "recommended": 0}]}
            self.store.event(self.sid, request)
            payload, ids = self.selected("run.needs_input")[-1]
            self.assertEqual(payload["input"], request)
            self.assertEqual(ids, ["owner"])
        self.assertEqual([p["event_id"] for p, _ in self.selected("run.needs_input")],
                         [f"{job['id']}:run.needs_input:first", f"{job['id']}:run.needs_input:second"])

    def test_required_confirmations_are_never_silenced_by_wording(self):
        self.run_job()
        for ui_id, title in (("routine", "Approve task for this session"),
                             ("effect", "Approve production deployment?")):
            self.store.event(self.sid, {"type": "extension_ui_request", "method": "confirm",
                                        "id": ui_id, "title": title})
        self.assertEqual([p["input_id"] for p, _ in self.selected("run.needs_input")],
                         ["routine", "effect"])

    def test_crash_abort_and_interruption_reach_owner_once(self):
        for status in ("error", "aborted", "interrupted"):
            job = self.run_job()
            self.finish(job, status, error={"message": "Stopped before completion"})
            self.finish(job, status)
            payload, ids = self.selected("run.error")[-1]
            self.assertEqual(ids, ["owner"])
            self.assertEqual((payload["status"], payload["error"], payload["message"]),
                             (status, "Stopped before completion", "Stopped before completion"))
        self.assertEqual(len(self.selected("run.error")), 3)

    def test_monitoring_never_wakes_conversation_subscribers(self):
        job = self.run_job()
        self.store.deadline(job["id"])
        self.now = 1800
        self.store.monitor_tick()
        self.store.notifications.emit("daemon.up")
        self.assertEqual([(p["event"], ids) for p, ids in self.enqueued],
                         [("run.deadline", ["monitor"]), ("run.stalled", ["monitor"]),
                          ("daemon.up", ["monitor"])])
        self.assertTrue(all(p["channel"] == "monitor" for p, _ in self.enqueued))

    def test_followup_uses_same_session_and_new_completion_identity(self):
        first = self.run_job()
        self.finish(first)
        second = self.run_job()
        self.finish(second)
        done = self.selected("run.done")
        self.assertEqual([p["session_id"] for p, _ in done], [self.sid, self.sid])
        self.assertEqual([p["event_id"] for p, _ in done],
                         [f"{first['id']}:run.done", f"{second['id']}:run.done"])

    def test_unknown_or_nonstring_coordinator_is_rejected_before_job_admission(self):
        for owner in ("not-configured", 42, []):
            with self.subTest(owner=owner):
                with self.assertRaisesRegex(ValueError, "coordinator_subscriber_id"):
                    self.run_job(owner)
                self.assertEqual(self.store.jobs, {})

    def test_restoring_completed_job_does_not_repeat_conversation(self):
        job = self.run_job()
        self.finish(job)
        self.store.close()
        self.enqueued.clear()
        restored = Store(self.root, self.dispatcher, clock=lambda: self.now)
        self.addCleanup(restored.close)
        self.assertEqual(restored.job(job["id"])["coordinator_subscriber_id"], "owner")
        self.assertEqual(restored.job(job["id"])["status"], "completed")
        self.assertEqual(self.enqueued, [])


if __name__ == "__main__":
    unittest.main()
