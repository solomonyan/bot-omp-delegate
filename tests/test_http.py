import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from bot_omp_delegate.cli import Client
from bot_omp_delegate.lifecycle import Store
from bot_omp_delegate.server import Handler


class HttpLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = self.root / "private-state"
        self.store = Store(self.state)
        token = "synthetic-http-test-credential"
        (self.state / "token").write_text(token)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.store = self.store
        self.server.token = token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(self.state, self.server.server_port)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def command(self, *args):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "bot_omp_delegate.cli",
             "--state-dir", str(self.state), "--port", str(self.server.server_port), *args],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_cli_lane_new_and_session_archive_preserve_prior_conversation(self):
        lane = self.command("lane", "create", "--name", "coding", "--cwd", str(self.project))
        session = self.command("session", "show", lane["sessionId"])
        self.assertEqual(session["worker"]["state"], "stopped")
        transcript = Path(session["sessionDir"]) / "retained.jsonl"
        transcript.write_text('{"role":"user","content":"keep this conversation"}\n')
        fresh = self.command("lane", "new", lane["id"])
        self.assertNotEqual(fresh["sessionId"], session["id"])
        self.assertIn(session["id"], fresh["history"])
        self.assertEqual(transcript.read_text(), '{"role":"user","content":"keep this conversation"}\n')
        self.command("session", "archive", session["id"])
        listed = self.command("session", "list")
        self.assertNotIn(session["id"], [s["id"] for s in listed["sessions"]])
        archived = self.command("session", "list", "--archived")
        self.assertIn(session["id"], [s["id"] for s in archived["sessions"]])
        self.command("session", "unarchive", session["id"])
        self.assertEqual(self.command("session", "show", session["id"])["availability"], "available")

    def test_archived_lane_rejects_dispatch_without_starting_worker(self):
        lane = self.client.request("POST", "/lanes", {"name": "blocked", "cwd": str(self.project)})
        self.client.request("POST", f'/sessions/{lane["sessionId"]}/archive', {})
        with self.assertRaisesRegex(RuntimeError, "409"):
            self.client.request("POST", f'/lanes/{lane["id"]}/run', {"message": "must not execute"})
        after = self.client.request("GET", f'/lanes/{lane["id"]}')
        self.assertIsNone(after["activeJob"])
        self.assertEqual(after["worker"]["state"], "stopped")
        self.assertEqual(self.store.jobs, {})

    def test_delete_confirmation_and_scope_preserve_project_and_other_session(self):
        project_file = self.project / "owned-by-project.txt"
        project_file.write_text("preserve")
        lane = self.client.request("POST", "/lanes", {"name": "retention", "cwd": str(self.project)})
        old = self.client.request("GET", f'/sessions/{lane["sessionId"]}')
        data_file = Path(old["sessionDir"]) / "private-transcript.jsonl"
        data_file.write_text("old private content")
        current = self.client.request("POST", f'/lanes/{lane["id"]}/new', {})
        self.client.request("POST", f'/sessions/{old["id"]}/archive', {})
        with self.assertRaisesRegex(RuntimeError, "400"):
            self.client.request("POST", f'/sessions/{old["id"]}/delete', {"confirmId": current["sessionId"]})
        self.assertTrue(data_file.exists())
        deleted = self.command("session", "delete", old["id"], "--confirm-id", old["id"])
        self.assertTrue(deleted["deleted"])
        self.assertFalse(Path(old["sessionDir"]).exists())
        self.assertEqual(project_file.read_text(), "preserve")
        self.assertEqual(self.command("lane", "show", lane["id"])["sessionId"], current["sessionId"])
        self.assertEqual(self.command("session", "show", current["sessionId"])["availability"], "available")

    def test_wait_preserves_terminal_provider_error_over_http(self):
        from unittest.mock import patch
        session = self.store.create({"name": "waiting", "cwd": str(self.project)})
        with patch.object(self.store, "_run"):
            job = self.store.turn(session["id"], "synthetic task")
        self.store.event(session["id"], {"type": "prompt_result", "id": job["id"],
                         "status": "error", "error": {"message": "provider unavailable"},
                         "sessionSettled": True})
        result = self.client.request("GET", f'/jobs/{job["id"]}/wait?timeout=1')
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "provider unavailable")


if __name__ == "__main__":
    unittest.main()
