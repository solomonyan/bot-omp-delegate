from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from bot_omp_delegate.cli import Client, _await_shutdown
from bot_omp_delegate.lifecycle import Store
from bot_omp_delegate.server import Handler
from bot_omp_delegate.webhooks import WebhookDispatcher


class WebhookHttpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.received = queue.Queue()
        received = self.received
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.put(payload)
                self.send_response(204)
                self.end_headers()
        self.receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        threading.Thread(target=self.receiver.serve_forever, daemon=True).start()
        self.addCleanup(self.receiver.server_close)
        self.addCleanup(self.receiver.shutdown)
        self.url = f"http://127.0.0.1:{self.receiver.server_port}/events"

    def cli(self, state, port, *args, input=None, expected=0):
        result = subprocess.run([sys.executable, "-B", "-m", "bot_omp_delegate.cli", "--state-dir", str(state), "--port", str(port), *args],
                                cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                                input=input, capture_output=True, text=True, timeout=20)
        self.assertTrue(self.url not in result.stdout + result.stderr, "CLI output must omit subscriber endpoints")
        self.assertTrue("synthetic-secret-value" not in result.stdout + result.stderr, "CLI output must omit subscriber credentials")
        self.assertEqual(result.returncode, expected, "Unexpected CLI exit (output omitted)")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def stop_daemon(self, state, port):
        try:
            Client(state, port).request("POST", "/shutdown", {"idleOnly": True})
            _await_shutdown(state)
        except RuntimeError:
            pass

    def test_real_daemon_startup_and_stdin_provisioning_test_remove(self):
        state = self.root / "daemon-state"
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        subscriber = {"id": "receiver", "url": self.url, "auth_header": "X-Token", "auth_value": "synthetic-secret-value", "events": ["daemon.up"]}
        added = self.cli(state, port, "webhook", "add", input=json.dumps(subscriber))
        self.assertFalse(added["runtime_reload"])
        self.assertEqual((state / "webhooks.json").stat().st_mode & 0o777, 0o600)
        self.addCleanup(self.stop_daemon, state, port)
        self.cli(state, port, "daemon", "start")
        startup = self.received.get(timeout=3)
        self.assertEqual((startup["event"], startup["version"], startup["status"]), ("daemon.up", 1, "up"))
        self.assertIsNotNone(datetime.fromisoformat(startup["ts"]).utcoffset())
        self.assertTrue(all(startup[key] == "" for key in ("lane", "lane_id", "job_id", "cwd", "branch", "last_commit_sha")))
        receipt = self.cli(state, port, "webhook", "test", "--event", "run.done")
        self.assertEqual((receipt["configured"], receipt["delivered"], receipt["results"][0]["status_code"]), (1, 1, 204))
        self.assertEqual(self.received.get(timeout=3)["event"], "run.done", "Explicit connectivity tests bypass subscriber filters")
        removed = self.cli(state, port, "webhook", "remove", "receiver")
        self.assertTrue(removed["runtime_reload"])
        self.assertEqual(self.cli(state, port, "webhook", "test"), {"configured": 0, "delivered": 0, "results": []})
        self.cli(state, port, "daemon", "restart", "--idle-only")
        self.cli(state, port, "daemon", "stop", "--idle-only")

    def test_foreground_deadline_and_labeled_completion_reach_receiver(self):
        state = self.root / "http-state"
        hub = WebhookDispatcher(state, retry_delays=())
        store = Store(state, hub)
        self.addCleanup(hub.close)
        self.addCleanup(store.close)
        token = "synthetic-webhook-http-token"
        (state / "token").write_text(token)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.store, server.webhooks, server.token = store, hub, token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port
        self.cli(state, port, "webhook", "add", input=json.dumps({"id": "coordinator", "url": self.url}))
        lane = self.cli(state, port, "lane", "create", "--name", "coding", "--cwd", str(self.project))
        with patch.object(store, "_run"):
            job = self.cli(state, port, "lane", "run", lane["id"], "work", "--label", "OPS-42", "--detach")
        self.cli(state, port, "wait", job["id"], "--max-hours", "0.000001", expected=3)
        deadline = self.received.get(timeout=3)
        self.assertEqual((deadline["event"], deadline["job_id"], deadline["label"]), ("run.deadline", job["id"], "OPS-42"))
        self.assertEqual(store.job(job["id"])["status"], "running")
        store.event(lane["sessionId"], {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "Change complete"}]}})
        store.event(lane["sessionId"], {"type": "prompt_result", "id": job["id"], "status": "completed", "sessionSettled": True})
        done = self.received.get(timeout=3)
        self.assertEqual((done["event"], done["summary"], done["label"], done["lane_id"]), ("run.done", "Change complete", "OPS-42", lane["id"]))
        self.assertEqual(done["cwd"], str(self.project.resolve()))


if __name__ == "__main__":
    unittest.main()
