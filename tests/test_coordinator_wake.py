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
        specs = {
            "melchior-lane-events": {"events": ["run.error", "run.deadline", "run.needs_input", "run.stalled"]},
            "melchior-fail-completions": {"events": ["run.done"]},
            "spawning-coordinator": {"events": ["daemon.up"], "lanes": ["different-lane"]},
            "other-coordinator": {"events": ["run.done"], "lanes": ["different-lane"]},
        }
        for subscriber_id, filters in specs.items():
            add_subscriber(self.root, {
                "id": subscriber_id,
                "url": "https://example.invalid/hook",
                **filters,
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

    def run_job(self, coordinator_subscriber_id=None):
        with patch.object(self.store, "_run"):
            return self.store.lane_action(self.lane["id"], "run", {
                "message": "work",
                "coordinator_subscriber_id": coordinator_subscriber_id,
            })

    def finish(self, job, status="completed", **extra):
        self.store.event(self.sid, {
            "type": "prompt_result", "id": job["id"], "status": status,
            "sessionSettled": True, **extra,
        })

    def selected(self, event):
        return [(payload, ids) for payload, ids in self.enqueued if payload["event"] == event]

    def test_healthy_completion_wakes_only_recorded_owner_once(self):
        job = self.run_job("spawning-coordinator")
        self.finish(job)
        done = self.selected("run.done")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][1], ["spawning-coordinator"])
        self.assertNotIn("other-coordinator", done[0][1])
        self.finish(job)
        self.store.event(self.sid, {"type": "session_settled"})
        self.assertEqual(len(self.selected("run.done")), 1)

    def test_healthy_completion_owned_by_gate_selects_gate_once(self):
        job = self.run_job("melchior-fail-completions")
        self.finish(job)
        self.assertEqual(self.selected("run.done")[0][1], ["melchior-fail-completions"])

    def test_no_owner_adds_no_completion_wake(self):
        job = self.run_job()
        self.finish(job)
        self.assertEqual(self.selected("run.done")[0][1], [])

    def test_noncompletion_events_keep_lane_routes_and_routine_approval_pending(self):
        errored = self.run_job("spawning-coordinator")
        self.finish(errored, status="error", error={"message": "provider failed"})
        self.assertEqual(self.selected("run.error")[-1][1], ["melchior-lane-events"])

        deadline = self.run_job("spawning-coordinator")
        self.store.deadline(deadline["id"])
        self.assertEqual(self.selected("run.deadline")[-1][1], ["melchior-lane-events"])
        self.finish(deadline)

        question = self.run_job("spawning-coordinator")
        approvals = [
            {"type": "extension_ui_request", "method": "confirm", "id": "approval",
             "title": "Approve task for this session"},
            {"type": "extension_ui_request", "method": "confirm", "id": "eval-approval",
             "title": "Allow eval for this session"},
            {"type": "extension_ui_request", "method": "confirm", "id": "tool-approval",
             "title": "Allow-tool task for the session?"},
        ]
        for approval in approvals:
            self.store.event(self.sid, approval)
        self.assertEqual(self.store.job(question["id"])["status"], "waiting_input")
        self.assertEqual(self.store.job(question["id"])["pendingInput"], approvals)
        self.assertEqual(self.selected("run.needs_input"), [])
        for approval in approvals:
            self.store.event(self.sid, {"type": "extension_ui_request", "method": "cancel",
                                        "targetId": approval["id"]})
        self.finish(question)

        human_question = self.run_job("spawning-coordinator")
        self.store.event(self.sid, {"type": "extension_ui_request", "method": "input", "id": "human",
                                    "title": "Which ticket?"})
        self.assertEqual(self.selected("run.needs_input")[-1][1], ["melchior-lane-events"])

        self.store.event(self.sid, {"type": "extension_ui_request", "method": "cancel", "targetId": "human"})
        self.now = 1800
        self.store.monitor_tick()
        self.assertEqual(self.selected("run.stalled")[-1][1], ["melchior-lane-events"])
        self.assertEqual(self.store.job(human_question["id"])["status"], "running")

    def test_unknown_confirmations_and_human_questions_are_not_approval_filtered(self):
        self.run_job("spawning-coordinator")
        for index, (method, title) in enumerate([
            ("confirm", "Approve production deployment?"),
            ("confirm", "Approve this callback change?"),
            ("ask", "Approve task for this session"),
            ("confirm", "Approve this call  bash rm -rf /some/path"),
        ]):
            self.store.event(self.sid, {"type": "extension_ui_request", "method": method,
                                        "id": f"human-{index}", "title": title})
        decisions = self.selected("run.needs_input")
        self.assertEqual([payload["summary"] for payload, _ in decisions],
                         ["Approve production deployment?", "Approve this callback change?",
                          "Approve task for this session", "Approve this call  bash rm -rf /some/path"])
        self.assertTrue(all(ids == ["melchior-lane-events"] for _, ids in decisions))

    def test_session_approval_with_external_effect_context_still_escalates(self):
        self.run_job("spawning-coordinator")
        contexts = [
            {"message": "spend approval required"},
            {"message": "deploy to production"},
            {"message": "merge this change"},
            {"questions": [{"question": "Which secret may the tool access?"}]},
        ]
        for index, context in enumerate(contexts):
            self.store.event(self.sid, {"type": "extension_ui_request", "method": "confirm",
                                        "id": f"sensitive-{index}",
                                        "title": "Approve task for this session", **context})
        decisions = self.selected("run.needs_input")
        self.assertEqual(len(decisions), len(contexts))
        self.assertTrue(all(ids == ["melchior-lane-events"] for _, ids in decisions))

    def test_completed_failure_text_still_routes_as_done(self):
        job = self.run_job("spawning-coordinator")
        self.store.event(self.sid, {"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "FAIL: task completed with a reported failure"},
        ]}})
        self.finish(job)
        done = self.selected("run.done")[-1]
        self.assertIn("FAIL: task completed with a reported failure", done[0]["summary"])
        self.assertEqual(done[1], ["melchior-fail-completions", "spawning-coordinator"])

    def test_ownerless_completed_failure_still_reaches_gate(self):
        job = self.run_job()
        self.store.event(self.sid, {"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Verdict: FAIL"},
        ]}})
        self.finish(job)
        payload, ids = self.selected("run.done")[-1]
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(ids, ["melchior-fail-completions"])

    def test_approved_failure_grammar_and_first_verdict_precedence(self):
        cases = [
            ("", False),
            ("An earlier attempt failed; the fix is now verified.", False),
            ("Do not fail the completed task.", False),
            ("Notes\n  vErDiCt: fAiL\nDetails", True),
            ("Verdict: FAIL\nVerdict: PASS", True),
            ("Verdict: PASS\nVerdict: FAIL", False),
            ("Verdict: PASS+NOTES\nVerdict: FAIL\nBLOCKED", False),
            ("FAIL: earlier attempt\nVerdict: PASS", False),
            ("FAIL: task completed with a reported failure", True),
            ("Details\n  fail: verification missing", True),
            ("Quoted inline FAIL: is not a line-leading marker", False),
            ("FAIL — P0", True),
            ("fail – p1", True),
            ("FAIL - P3", True),
            ("FAIL - P4", False),
            ("FAIL - P30", False),
            ("Work is bLoCkEd pending input", True),
            ("Work is unblocked", False),
            ("ready.MD withheld pending verification", True),
            ("READYxmd withheld", False),
            ("Narrative Verdict: PASS\nFAIL: verification failed", True),
            ("Verdict: FAILURE", False),
        ]
        for summary, failed in cases:
            with self.subTest(summary=summary):
                job = self.run_job()
                self.store.event(self.sid, {"type": "message_end", "message": {
                    "role": "assistant", "content": [{"type": "text", "text": summary}],
                }})
                self.finish(job)
                payload, ids = self.selected("run.done")[-1]
                self.assertEqual(payload["status"], "completed")
                self.assertEqual(ids, ["melchior-fail-completions"] if failed else [])

    def test_completion_gate_uses_truncated_payload_summary(self):
        for prefix_length, failed in [(294, True), (295, False), (300, False)]:
            with self.subTest(prefix_length=prefix_length):
                summary = "x" * prefix_length + "\nFAIL:"
                envelope, subscribers = self.dispatcher._prepare({"event": "run.done", "summary": summary})
                self.assertEqual(envelope.payload["summary"], summary[:300])
                self.assertEqual([s["id"] for s in subscribers],
                                 ["melchior-fail-completions"] if failed else [])

    def test_completion_gate_uses_redacted_payload_summary(self):
        add_subscriber(self.root, {"id": "redaction-source", "url": "https://example.invalid/hook",
                                   "events": ["daemon.up"], "hmac_secret": "BLOCKED"})
        self.dispatcher.reload()
        envelope, subscribers = self.dispatcher._prepare({"event": "run.done", "summary": "BLOCKED"})
        self.assertEqual(envelope.payload["summary"], "[redacted]")
        self.assertEqual(subscribers, [])

    def test_generic_completion_subscribers_remain_selected_for_missing_summary(self):
        add_subscriber(self.root, {"id": "generic-audit", "url": "https://example.invalid/hook",
                                   "events": ["run.done"], "lanes": ["coding"]})
        self.dispatcher.reload()
        _, subscribers = self.dispatcher._prepare({"event": "run.done", "lane": "coding"})
        self.assertEqual([s["id"] for s in subscribers], ["generic-audit"])

    def test_completed_failure_owner_is_gate_is_not_duplicated(self):
        job = self.run_job("melchior-fail-completions")
        self.store.event(self.sid, {"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "Verdict: FAIL"}],
        }})
        self.finish(job)
        self.assertEqual(self.selected("run.done")[0][1], ["melchior-fail-completions"])

    def test_unknown_or_nonstring_coordinator_is_rejected_before_job_admission(self):
        for coordinator_id in ("not-configured", 42, []):
            with self.subTest(coordinator_id=coordinator_id):
                with self.assertRaisesRegex(ValueError, "coordinator_subscriber_id"):
                    self.run_job(coordinator_id)
                self.assertEqual(self.store.jobs, {})

    def test_restoring_completed_job_does_not_emit_completion_again(self):
        job = self.run_job("spawning-coordinator")
        self.assertEqual(job["coordinator_subscriber_id"], "spawning-coordinator")
        self.assertEqual(self.store.job(job["id"])["coordinator_subscriber_id"], "spawning-coordinator")
        self.finish(job)
        self.assertEqual(len(self.selected("run.done")), 1)
        self.store.close()
        self.enqueued.clear()
        restored = Store(self.root, self.dispatcher, clock=lambda: self.now)
        self.addCleanup(restored.close)
        self.assertEqual(restored.job(job["id"])["status"], "completed")
        self.assertEqual(restored.job(job["id"])["coordinator_subscriber_id"], "spawning-coordinator")
        self.assertEqual(self.enqueued, [])


if __name__ == "__main__":
    unittest.main()
