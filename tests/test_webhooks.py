import hashlib
import hmac
import http.server
import json
import os
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from bot_omp_delegate.webhooks import ConfigError, WebhookDispatcher, add_subscriber, list_config, load_config, remove_subscriber


class Receiver(http.server.BaseHTTPRequestHandler):
    response_status = 204
    received = []
    delay = 0

    wake = threading.Event()
    paths = []
    redirect = None
    def do_POST(self):
        if self.delay:
            time.sleep(self.delay)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.received.append((body, dict(self.headers)))
        self.paths.append(self.path)
        self.wake.set()
        self.send_response(self.response_status)
        if self.redirect:
            self.send_header("Location", self.redirect)
        self.end_headers()

    def log_message(self, *_args):
        pass


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.root.mkdir(mode=0o700)
        Receiver.received = []
        Receiver.response_status = 204
        Receiver.delay = 0
        Receiver.wake = threading.Event()
        Receiver.paths = []
        Receiver.redirect = None
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Receiver)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def subscriber(self, **kwargs):
        return {"id": "test", "url": self.url, **kwargs}

    def test_config_permissions_safe_view_and_remove(self):
        self.assertEqual(load_config(self.root)["version"], 1)
        add_subscriber(self.root, self.subscriber(auth_header="Authorization", auth_value="private-token", hmac_secret="signing-secret"))
        self.assertEqual((self.root / "webhooks.json").stat().st_mode & 0o777, 0o600)
        rendered = repr(list_config(self.root))
        self.assertNotIn(self.url, rendered)
        self.assertNotIn("private-token", rendered)
        self.assertNotIn("signing-secret", rendered)
        remove_subscriber(self.root, "test")
        self.assertEqual(list_config(self.root)["subscribers"], [])

    def test_https_dns_configuration_and_default_isolation(self):
        first = load_config(self.root)
        first["subscribers"].append({"id": "only-in-memory"})
        self.assertEqual(load_config(self.root)["subscribers"], [])
        add_subscriber(self.root, {"id": "secure", "url": "https://receiver.example/hook"})
        self.assertEqual(list_config(self.root)["subscribers"][0]["id"], "secure")

    def test_rejects_symlink_and_insecure_mode(self):
        elsewhere = self.root / "other"
        elsewhere.write_text(json.dumps({"version": 1, "stall_seconds": 1, "idle_seconds": None, "subscribers": []}))
        (self.root / "webhooks.json").symlink_to(elsewhere)
        with self.assertRaises(ConfigError):
            load_config(self.root)
        (self.root / "webhooks.json").unlink()
        (self.root / "webhooks.json").write_text(json.dumps({"version": 1, "stall_seconds": 1, "idle_seconds": None, "subscribers": []}))
        os.chmod(self.root / "webhooks.json", 0o644)
        with self.assertRaises(ConfigError):
            load_config(self.root)

    def test_delivery_hmac_and_event_filter(self):
        add_subscriber(self.root, self.subscriber(events=["run.done"], hmac_secret="synthetic-key"))
        dispatcher = WebhookDispatcher(self.root, retry_delays=(0,), request_timeout=1)
        dispatcher.emit({"event": "run.error", "summary": "ignored"})
        time.sleep(.1)
        self.assertEqual(Receiver.received, [])
        result = dispatcher.test()
        self.assertEqual(result["delivered"], 1)
        body, headers = Receiver.received[-1]
        self.assertEqual(headers["X-OMP-Signature"], "sha256=" + hmac.new(b"synthetic-key", body, hashlib.sha256).hexdigest())
        self.assertEqual(headers["X-OMP-Event"], "daemon.up")
        dispatcher.close()

    def test_retries_non_2xx_and_success(self):
        add_subscriber(self.root, self.subscriber())
        Receiver.response_status = 503
        dispatcher = WebhookDispatcher(self.root, retry_delays=(0, 0, 0), request_timeout=1)
        failed = dispatcher.test()
        self.assertEqual(failed["results"][0]["attempts"], 4)
        self.assertEqual(len({headers["X-OMP-Delivery"] for _, headers in Receiver.received}), 1)
        self.assertEqual(len({body for body, _ in Receiver.received}), 1)
        self.assertEqual(failed["results"][0]["error"], "http_error")
        Receiver.response_status = 204
        success = dispatcher.test()
        self.assertTrue(success["results"][0]["delivered"])
        dispatcher.close()

    def test_emit_is_nonblocking_and_no_subscribers_is_noop(self):
        dispatcher = WebhookDispatcher(self.root)
        self.assertEqual(dispatcher.test(), {"configured": 0, "delivered": 0, "results": []})
        self.assertIsNone(dispatcher.emit({"event": "daemon.up"}))
        dispatcher.close()

    def test_slow_delivery_does_not_block_emit_and_receipt_is_sanitized(self):
        add_subscriber(self.root, self.subscriber())
        Receiver.delay = .3
        dispatcher = WebhookDispatcher(self.root, retry_delays=(), request_timeout=1)
        started = time.monotonic()
        dispatcher.emit({"event": "daemon.up", "summary": self.url})
        self.assertLess(time.monotonic() - started, .1)
        receipt = dispatcher.test()
        self.assertEqual(receipt["delivered"], 1)
        self.assertNotIn(self.url, repr(receipt))
        dispatcher.close()

    def test_event_and_lane_filters_match_name_or_id(self):
        add_subscriber(self.root, self.subscriber(events=["run.done"], lanes=["coding"]))
        add_subscriber(self.root, self.subscriber(id="by-id", events=["run.error"], lanes=["lane-42"]))
        dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(dispatcher.close)
        dispatcher.emit({"event": "run.done", "lane": "other", "lane_id": "other"})
        dispatcher.emit({"event": "run.error", "lane": "coding", "lane_id": "other"})
        dispatcher.emit({"event": "run.done", "lane": "coding", "lane_id": "other"})
        dispatcher.emit({"event": "run.error", "lane": "other", "lane_id": "lane-42"})
        dispatcher._queue.join()
        self.assertCountEqual([(json.loads(body)["event"], json.loads(body)["lane_id"]) for body, _ in Receiver.received],
                              [("run.done", "other"), ("run.error", "lane-42")])

    def test_redaction_precedes_summary_truncation_and_git_uses_event_worktree(self):
        secret = "synthetic-authentication-secret-crossing-the-summary-boundary"
        add_subscriber(self.root, self.subscriber(auth_header="X-Token", auth_value=secret))
        project = self.root.parent / "repository"
        project.mkdir()
        def git(*args):
            result = subprocess.run(["git", "-C", str(project), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                                    check=True, capture_output=True, text=True, timeout=5)
            return result.stdout.strip()
        git("init", "-b", "event-check")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture")
        expected_sha = git("rev-parse", "HEAD")
        dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(dispatcher.close)
        dispatcher.emit({"event": "run.done", "cwd": str(project), "summary": "x" * 290 + secret, "label": "OPS-42"})
        dispatcher._queue.join()
        body, headers = Receiver.received[0]
        payload = json.loads(body)
        self.assertEqual(payload["summary"], "x" * 290 + "[redacted]")
        self.assertEqual((payload["version"], payload["branch"], payload["last_commit_sha"], payload["label"]),
                         (1, "event-check", expected_sha, "OPS-42"))
        self.assertEqual(headers["X-Token"], secret)

    def test_failed_receiver_retries_independently_of_healthy_subscriber(self):
        class Failing(Receiver):
            response_status = 503
            received, paths = [], []
            wake = threading.Event()
        failed_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Failing)
        threading.Thread(target=failed_server.serve_forever, daemon=True).start()
        self.addCleanup(failed_server.server_close)
        self.addCleanup(failed_server.shutdown)
        add_subscriber(self.root, self.subscriber(id="bad", url=f"http://127.0.0.1:{failed_server.server_port}/fail"))
        add_subscriber(self.root, self.subscriber(id="healthy"))
        dispatcher = WebhookDispatcher(self.root, retry_delays=(.2, .2, .2), request_timeout=1)
        self.addCleanup(dispatcher.close)
        before = time.monotonic()
        dispatcher.emit({"event": "daemon.up"})
        self.assertLess(time.monotonic() - before, .2)
        self.assertTrue(Receiver.wake.wait(.5), "Healthy subscriber must not wait for another receiver's retries")
        dispatcher._queue.join()
        self.assertEqual(len(Failing.received), 4)
        self.assertEqual(len(Receiver.received), 1)

    def test_full_queue_test_is_nonblocking_and_sanitized(self):
        add_subscriber(self.root, self.subscriber())
        dispatcher = WebhookDispatcher(self.root, workers=1, queue_size=1, retry_delays=())
        entered, release = threading.Event(), threading.Event()
        def slow_request(*args):
            entered.set()
            release.wait(3)
            return True, 204, None
        try:
            with patch("bot_omp_delegate.webhooks._request", side_effect=slow_request):
                dispatcher.emit({"event": "daemon.up"})
                self.assertTrue(entered.wait(1))
                dispatcher.emit({"event": "daemon.up"})
                before = time.monotonic()
                receipt = dispatcher.test()
                self.assertLess(time.monotonic() - before, .2)
                self.assertEqual((receipt["delivered"], receipt["results"][0]["error"]), (0, "queue_full"))
                release.set()
                dispatcher._queue.join()
        finally:
            release.set()
            dispatcher.close()

    def test_redirects_are_not_followed_and_proxy_environment_is_ignored(self):
        add_subscriber(self.root, self.subscriber())
        dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(dispatcher.close)
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "no_proxy": ""}):
            self.assertEqual(dispatcher.test()["delivered"], 1)
            Receiver.response_status = 307
            Receiver.redirect = self.url + "/unexpected"
            receipt = dispatcher.test()
        self.assertEqual((receipt["delivered"], receipt["results"][0]["status_code"]), (0, 307))
        self.assertEqual(Receiver.paths, ["/hook", "/hook"])

    def test_invalid_configuration_or_event_never_reports_successful_empty_test(self):
        add_subscriber(self.root, self.subscriber())
        os.chmod(self.root / "webhooks.json", 0o644)
        dispatcher = WebhookDispatcher(self.root)
        self.addCleanup(dispatcher.close)
        with self.assertRaises(ConfigError):
            dispatcher.test()
        with self.assertRaises(ConfigError):
            dispatcher.test("unknown")
        self.assertEqual(Receiver.received, [])


    def test_trickled_response_headers_have_an_absolute_socket_deadline(self):
        class Trickle(Receiver):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    self.connection.sendall(b"HTTP/1.1 204 ")
                    for _ in range(30):
                        self.connection.sendall(b" ")
                        time.sleep(.02)
                except OSError:
                    pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        add_subscriber(self.root, self.subscriber(url=f"http://127.0.0.1:{server.server_port}/trickle"))
        dispatcher = WebhookDispatcher(self.root, retry_delays=(), request_timeout=.1)
        self.addCleanup(dispatcher.close)
        before = time.monotonic()
        receipt = dispatcher.test()
        self.assertLess(time.monotonic() - before, .5)
        self.assertEqual((receipt["delivered"], receipt["results"][0]["error"]), (0, "network_error"))

    def test_graceful_close_gives_queued_shutdown_errors_a_bounded_delivery_chance(self):
        add_subscriber(self.root, self.subscriber())
        dispatcher = WebhookDispatcher(self.root, workers=1, retry_delays=())
        entered, release = threading.Event(), threading.Event()
        delivered = []
        def request(_subscriber, body, *_args):
            event = json.loads(body)["event"]
            if event == "daemon.up":
                entered.set()
                release.wait(2)
            delivered.append(event)
            return True, 204, None
        try:
            with patch("bot_omp_delegate.webhooks._request", side_effect=request):
                dispatcher.emit({"event": "daemon.up"})
                self.assertTrue(entered.wait(1))
                dispatcher.emit({"event": "run.error"})
                timer = threading.Timer(.05, release.set)
                timer.start()
                before = time.monotonic()
                dispatcher.close(drain_timeout=.5)
                timer.join()
                self.assertLess(time.monotonic() - before, 1.5)
                self.assertEqual(delivered, ["daemon.up", "run.error"])
        finally:
            release.set()
            dispatcher.close(drain_timeout=0)

    def test_invalid_timer_values_disable_notifications_without_crashing_startup(self):
        for duration in (0, -1, True, "1800", float("inf"), 10 ** 500):
            with self.subTest(type=type(duration).__name__):
                path = self.root / "webhooks.json"
                path.write_text(json.dumps({"version": 1, "stall_seconds": duration, "subscribers": []}))
                path.chmod(0o600)
                dispatcher = WebhookDispatcher(self.root)
                try:
                    self.assertFalse(dispatcher.enabled)
                    self.assertEqual(dispatcher.public_config()["config_error"], "invalid_configuration")
                    with self.assertRaises(ConfigError):
                        dispatcher.test()
                finally:
                    dispatcher.close()

if __name__ == "__main__":
    unittest.main()
