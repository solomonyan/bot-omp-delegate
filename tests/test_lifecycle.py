import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot_omp_delegate.lifecycle import Conflict, Store


class LifecycleBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.cwd = Path(self.temp.name) / "project"
        self.cwd.mkdir()
        self.store = Store(self.root)

    def test_additive_migration_preserves_legacy_session_and_job(self):
        migration_root = self.root / "legacy-state"
        (migration_root / "sessions").mkdir(parents=True)
        (migration_root / "jobs").mkdir()
        legacy = {"old-id": {"id": "old-id", "name": "old", "cwd": str(self.cwd), "options": {"model": "m"}, "activeJob": None, "sessionDir": str(migration_root / "sessions" / "old-id")}}
        (migration_root / "sessions.json").write_text(json.dumps(legacy))
        transcript = {"id": "old-job", "sessionId": "old-id", "status": "completed", "finalMessage": "retained"}
        (migration_root / "jobs" / "old-job.json").write_text(json.dumps(transcript))
        migrated = Store(migration_root)
        self.assertEqual(migrated.session("old-id")["options"], {"model": "m"})
        self.assertEqual(migrated.job("old-job")["finalMessage"], "retained")
        self.assertTrue((migration_root / "state.json").exists())
        self.assertFalse((migration_root / "sessions.json").exists())
        self.assertEqual(migrated.show_session("old-id")["worker"]["state"], "stopped")

    def test_lane_new_preserves_old_history_and_close_busy_conflicts(self):
        lane = self.store.create_lane({"name": "lane", "cwd": str(self.cwd)})
        old_id = lane["sessionId"]
        with patch.object(self.store, "_run"):
            job = self.store.lane_action(lane["id"], "run", {"message": "work"})
        with self.assertRaises(Conflict):
            self.store.lane_action(lane["id"], "close", {})
        self.store.event(old_id, {"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        fresh = self.store.lane_action(lane["id"], "new", {})
        self.assertNotEqual(fresh["sessionId"], old_id)
        self.assertEqual(fresh["history"], [old_id, fresh["sessionId"]])
        self.assertIn(old_id, self.store.sessions)

    def test_default_close_releases_idle_worker_and_preserves_session(self):
        lane = self.store.create_lane({"name": "lane", "cwd": str(self.cwd)})
        class Worker:
            pid = 123
            alive = True
            def close(self): self.alive = False
        worker = Worker()
        sid = lane["sessionId"]
        self.store.workers[sid] = worker
        result = self.store.lane_action(lane["id"], "close", {})
        self.assertFalse(worker.alive)
        self.assertNotIn(sid, self.store.workers)
        self.assertEqual(result["worker"]["state"], "stopped")
        self.assertEqual(result["sessionId"], sid)
        self.assertTrue(Path(self.store.session(sid)["sessionDir"]).is_dir())

    def test_assign_requires_revision_and_unique_lane_ownership(self):
        first = self.store.create_lane({"name": "first", "cwd": str(self.cwd)})
        second = self.store.create_lane({"name": "second", "cwd": str(self.cwd)})
        with self.assertRaises(Conflict):
            self.store.lane_action(first["id"], "assign", {"sessionId": second["sessionId"], "expectedRevision": first["revision"]})
        session = self.store.create({"name": "detached", "cwd": str(self.cwd)})
        with self.assertRaises(Conflict):
            self.store.lane_action(first["id"], "assign", {"sessionId": session["id"], "expectedRevision": 0})
        assigned = self.store.lane_action(first["id"], "assign", {"sessionId": session["id"], "expectedRevision": first["revision"]})
        self.assertEqual(assigned["sessionId"], session["id"])

    def test_archive_blocks_dispatch_and_delete_requires_archived_unassigned_session(self):
        lane = self.store.create_lane({"name": "lane", "cwd": str(self.cwd)})
        sid = lane["sessionId"]
        with self.assertRaises(Conflict):
            self.store.session_action(sid, "delete", {"confirmId": sid})
        self.store.session_action(sid, "archive", {})
        with self.assertRaises(Conflict):
            self.store.lane_action(lane["id"], "run", {"message": "no"})
        self.store.lane_action(lane["id"], "assign", {"sessionId": self.store.create({"name": "other", "cwd": str(self.cwd)})["id"], "expectedRevision": lane["revision"]})
        removed = self.store.session_action(sid, "delete", {"confirmId": sid})
        self.assertTrue(removed["deleted"])
        self.assertNotIn(sid, self.store.sessions)

    def test_purge_intent_recovers_partial_cleanup(self):
        session = self.store.create({"name": "purge", "cwd": str(self.cwd)})
        sid = session["id"]
        self.store.session_action(sid, "archive", {})
        self.store._save_state()
        (self.root / "purge.json").write_text(json.dumps({"sessionId": sid, "sessionDir": session["sessionDir"]}))
        restarted = Store(self.root)
        self.assertNotIn(sid, restarted.sessions)
        self.assertFalse((self.root / "purge.json").exists())

    def test_old_job_controls_rejected_after_worker_generation_changes(self):
        lane = self.store.create_lane({"name": "lane", "cwd": str(self.cwd)})
        sid = lane["sessionId"]
        with patch.object(self.store, "_run"):
            job = self.store.turn(sid, "work")
        self.store.session(sid)["worker"]["generation"] = "successor"
        with self.assertRaises(Conflict):
            self.store.control(job["id"], "steer", {"message": "wrong job"})

    def test_configure_merges_options_without_replacing_conversation(self):
        session = self.store.create({"name": "configured", "cwd": str(self.cwd), "options": {"model": "old"}})
        updated = self.store.session_action(session["id"], "configure", {"options": {"thinking": "high"}})
        self.assertEqual(updated["options"], {"model": "old", "thinking": "high"})
        self.assertEqual(updated["sessionDir"], session["sessionDir"])


    def test_new_reserves_lane_while_idle_worker_is_closing(self):
        import threading
        lane = self.store.create_lane({"name": "lane", "cwd": str(self.cwd)})
        entered = threading.Event()
        release = threading.Event()
        class Worker:
            alive = True
            pid = 10
            def close(self):
                entered.set()
                if not release.wait(3): raise RuntimeError("test close timed out")
                self.alive = False
        self.store.workers[lane["sessionId"]] = Worker()
        errors = []
        results = []
        def replace_conversation():
            try: results.append(self.store.lane_action(lane["id"], "new", {}))
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=replace_conversation)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(Conflict):
                self.store.lane_action(lane["id"], "run", {"message": "must not dispatch"})
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertNotEqual(results[0]["sessionId"], lane["sessionId"])

    def test_child_from_previous_task_cannot_be_read_as_successor_task(self):
        session = self.store.create({"name": "children", "cwd": str(self.cwd)})
        sid = session["id"]
        class Ticket:
            def result(self):
                return {"messages": [{"role": "assistant", "content": "prior child's private transcript"}]}
        class Worker:
            pid = 100
            alive = True
            def begin_request(self, *_args, **_kwargs): return Ticket()
        self.store.workers[sid] = Worker()
        self.store.ready.add(sid)
        self.store.session(sid)["worker"].update(state="idle", generation="same-worker")
        with patch.object(self.store, "_run"):
            first = self.store.turn(sid, "first task")
        self.store.event(sid, {"type": "subagent_lifecycle",
                         "payload": {"id": "old-child", "status": "started", "parentToolCallId": "first-tool"}})
        self.store.event(sid, {"type": "prompt_result", "id": first["id"],
                         "status": "completed", "sessionSettled": True})
        with patch.object(self.store, "_run"):
            successor = self.store.turn(sid, "successor task")
        with self.assertRaisesRegex(Conflict, "owned by this job"):
            self.store.agent_action(successor["id"], "read", {"subagentId": "old-child"})

    def test_failed_close_quarantines_worker_until_cleanup_succeeds(self):
        session = self.store.create({"name": "cleanup", "cwd": str(self.cwd)})
        sid = session["id"]
        class Worker:
            pid = 100
            alive = True
            def close(self): raise RuntimeError("cleanup unresolved")
        self.store.workers[sid] = Worker()
        self.store.ready.add(sid)
        self.store.session(sid)["worker"].update(state="idle", generation="quarantine")
        with self.assertRaisesRegex(RuntimeError, "cleanup unresolved"):
            self.store.session_action(sid, "close", {})
        with self.assertRaises(Conflict):
            with patch.object(self.store, "_run"):
                self.store.turn(sid, "must not reach quarantined writer")

    def test_interrupt_close_during_startup_prevents_late_prompt(self):
        import threading
        entered = threading.Event()
        release = threading.Event()
        dispatched = []
        class Worker:
            alive = False
            pid = None
            def __init__(self, *_args): pass
            def start(self):
                entered.set()
                if not release.wait(3): raise RuntimeError("startup test timed out")
                raise RuntimeError("startup cancelled")
            def close(self): release.set()
            def send_prompt(self, *_args): dispatched.append(True)
        session = self.store.create({"name": "startup", "cwd": str(self.cwd)})
        with patch("bot_omp_delegate.lifecycle.Worker", Worker):
            job = self.store.turn(session["id"], "must not dispatch")
            self.assertTrue(entered.wait(3))
            result = self.store.session_action(session["id"], "close", {"interrupt": True})
        self.assertEqual(result["worker"]["state"], "stopped")
        self.assertIsNone(result["activeJob"])
        self.assertEqual(self.store.job(job["id"])["status"], "aborted")
        self.assertEqual(dispatched, [])

    def test_delayed_control_reply_blocks_successor_until_acknowledged(self):
        import threading
        entered = threading.Event()
        release = threading.Event()
        class Ticket:
            def result(self):
                entered.set()
                if not release.wait(3): raise RuntimeError("control test timed out")
        class Worker:
            alive = True
            pid = 100
            def begin_request(self, *_args, **_kwargs): return Ticket()
        lane = self.store.create_lane({"name": "race", "cwd": str(self.cwd)})
        sid = lane["sessionId"]
        self.store.workers[sid] = Worker()
        self.store.ready.add(sid)
        self.store.session(sid)["worker"].update(state="idle", generation="control-generation")
        with patch.object(self.store, "_run"):
            first = self.store.turn(sid, "first task")
        errors = []
        def steer():
            try: self.store.control(first["id"], "steer", {"message": "first task only"})
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=steer)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.store.event(sid, {"type": "prompt_result", "id": first["id"],
                             "status": "completed", "sessionSettled": True})
            with self.assertRaises(Conflict):
                self.store.turn(sid, "must not become the steer target")
            with self.assertRaises(Conflict):
                self.store.lane_action(lane["id"], "new", {})
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with patch.object(self.store, "_run"):
            successor = self.store.turn(sid, "now safe")
        self.assertNotEqual(successor["id"], first["id"])
        with self.assertRaises(Conflict):
            self.store.control(first["id"], "interrupt", {})

    def test_prewarm_reserves_worktree_and_can_be_explicitly_cancelled(self):
        import threading
        entered = threading.Event()
        release = threading.Event()
        class Worker:
            alive = False
            pid = None
            def __init__(self, *_args): pass
            def start(self):
                entered.set()
                if not release.wait(3): raise RuntimeError("prewarm test timed out")
                raise RuntimeError("prewarm cancelled")
            def close(self): release.set()
        session = self.store.create({"name": "prewarm", "cwd": str(self.cwd)})
        sibling = self.store.create({"name": "sibling", "cwd": str(self.cwd)})
        errors = []
        def prewarm():
            try: self.store.session_action(session["id"], "open", {})
            except Exception as exc: errors.append(str(exc))
        with patch("bot_omp_delegate.lifecycle.Worker", Worker):
            thread = threading.Thread(target=prewarm)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(Conflict):
                    self.store.turn(sibling["id"], "must not enter reserved worktree")
                with self.assertRaises(Conflict):
                    self.store.compact(sibling["id"])
                with self.assertRaises(Conflict):
                    self.store.session_action(sibling["id"], "open", {})
                result = self.store.session_action(session["id"], "close", {"interrupt": True})
            finally:
                release.set()
                thread.join(3)
        self.assertEqual(result["worker"]["state"], "stopped")
        self.assertEqual(errors, ["prewarm cancelled"])
        self.assertFalse(thread.is_alive())

    def test_purge_does_not_follow_nested_or_top_level_session_symlinks(self):
        outside = self.cwd / "outside.txt"
        outside.write_text("preserve outside data")
        session = self.store.create({"name": "nested-link", "cwd": str(self.cwd)})
        directory = Path(session["sessionDir"])
        (directory / "link").symlink_to(outside)
        self.store.session_action(session["id"], "archive", {})
        self.store.session_action(session["id"], "delete", {"confirmId": session["id"]})
        self.assertEqual(outside.read_text(), "preserve outside data")
        other = self.store.create({"name": "top-link", "cwd": str(self.cwd)})
        directory = Path(other["sessionDir"])
        self.store.session_action(other["id"], "archive", {})
        directory.rmdir()
        directory.symlink_to(self.cwd, target_is_directory=True)
        with self.assertRaises(Conflict):
            self.store.session_action(other["id"], "delete", {"confirmId": other["id"]})
        self.assertEqual(outside.read_text(), "preserve outside data")
        self.assertIn(other["id"], self.store.sessions)

    def test_partial_purge_recovers_after_session_metadata_was_removed(self):
        session = self.store.create({"name": "partial-purge", "cwd": str(self.cwd)})
        directory = Path(session["sessionDir"])
        (directory / "transcript.jsonl").write_text("private fixture")
        with patch.object(self.store, "_run"):
            job = self.store.turn(session["id"], "fixture task")
        self.store.event(session["id"], {"type": "prompt_result", "id": job["id"],
                         "status": "completed", "sessionSettled": True})
        self.store.session_action(session["id"], "archive", {})
        with patch("bot_omp_delegate.lifecycle.shutil.rmtree", side_effect=OSError("fixture cleanup interrupted")):
            with self.assertRaises(OSError):
                self.store.session_action(session["id"], "delete", {"confirmId": session["id"]})
        self.assertTrue((self.root / "purge.json").exists())
        restarted = Store(self.root)
        self.assertNotIn(session["id"], restarted.sessions)
        self.assertNotIn(job["id"], restarted.jobs)
        self.assertFalse(directory.exists())
        self.assertFalse((self.root / "purge.json").exists())

if __name__ == "__main__":
    unittest.main()
