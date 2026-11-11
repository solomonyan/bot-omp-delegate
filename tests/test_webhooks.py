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
        dispatcher.emit({"event": "run.stalled", "summary": self.url})
        self.assertLess(time.monotonic() - started, .1)
        receipt = dispatcher.test()
        self.assertEqual(receipt["delivered"], 1)
        self.assertNotIn(self.url, repr(receipt))
        dispatcher.close()

    def test_event_and_lane_filters_match_name_or_id(self):
        add_subscriber(self.root, self.subscriber(events=["run.stalled"], lanes=["coding"]))
        add_subscriber(self.root, self.subscriber(id="by-id", events=["run.stalled"], lanes=["lane-42"]))
        dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(dispatcher.close)
        dispatcher.emit({"event": "run.stalled", "lane": "other", "lane_id": "other"})
        dispatcher.emit({"event": "lane.idle", "lane": "coding", "lane_id": "other"})
        dispatcher.emit({"event": "run.stalled", "lane": "coding", "lane_id": "other"})
        dispatcher.emit({"event": "run.stalled", "lane": "other", "lane_id": "lane-42"})
        dispatcher._queue.join()
        self.assertCountEqual([(json.loads(body)["event"], json.loads(body)["lane_id"]) for body, _ in Receiver.received],
                              [("run.stalled", "other"), ("run.stalled", "lane-42")])

    def test_mode_defaults_to_monitor_and_rejects_unknown_values(self):
        add_subscriber(self.root, self.subscriber())
        self.assertEqual(list_config(self.root)["subscribers"][0]["mode"], "monitor")
        with self.assertRaises(ConfigError):
            add_subscriber(self.root, self.subscriber(id="bad-mode", mode="chatty"))
        add_subscriber(self.root, self.subscriber(id="conversational", mode="conversation"))
        self.assertEqual([s["mode"] for s in list_config(self.root)["subscribers"]], ["monitor", "conversation"])

    def test_conversational_events_reach_only_the_recorded_owner(self):
        # The owner's own event filters exclude run.done; the second subscriber
        # filter-matches run.done but is never the owner.
        add_subscriber(self.root, self.subscriber(events=["run.deadline"]))
        add_subscriber(self.root, self.subscriber(id="filter-match", events=["run.done"]))
        dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(dispatcher.close)
        # Owner selection overrides the owner's filters; a filter-matching
        # non-owner still receives nothing.
        dispatcher.emit({"event": "run.done", "summary": "owned"}, coordinator_subscriber_id="test")
        # Ownerless conversational events never broadcast.
        dispatcher.emit({"event": "run.error", "summary": "ownerless"})
        dispatcher._queue.join()
        self.assertEqual(len(Receiver.received), 1)
        self.assertEqual(json.loads(Receiver.received[0][0])["summary"], "owned")

    def test_operational_events_use_filters_and_never_reach_conversation_subscribers(self):
        add_subscriber(self.root, self.subscriber(events=["run.stalled"]))
        add_subscriber(self.root, self.subscriber(id="conversational", mode="conversation"))
        dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(dispatcher.close)
        dispatcher.emit({"event": "run.stalled", "lane": "coding"})
        dispatcher.emit({"event": "lane.idle", "lane": "coding"})
        # A mismatched channel must not broadcast a conversational event.
        dispatcher.emit({"event": "run.done", "channel": "monitor", "lane": "coding"})
        dispatcher._queue.join()
        self.assertEqual([json.loads(body)["event"] for body, _ in Receiver.received],
                         ["run.stalled"])

    def with_config(self, **keys):
        path = self.root / "webhooks.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **keys}))
        os.chmod(path, 0o600)

    def filtered(self):
        path = self.root / "logs" / "events-filtered.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_label_routes_to_mapped_owner_and_unmapped_label_falls_back_to_coordinator(self):
        add_subscriber(self.root, self.subscriber(id="jarvis-omp-events", url=self.url.replace("/hook", "/jarvis"), mode="conversation"))
        add_subscriber(self.root, self.subscriber(id="friday-omp-events", url=self.url.replace("/hook", "/friday"), mode="conversation"))
        self.with_config(caller_routes={"omp-caller:friday": "friday-omp-events", "omp-caller:gone": "removed-subscriber"})
        dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(dispatcher.close)
        for label in ("omp-caller:friday", "omp-caller:karen", "omp-caller:gone"):
            dispatcher.emit({"event": "run.done", "label": label, "summary": label}, coordinator_subscriber_id="jarvis-omp-events")
        dispatcher.emit({"event": "run.stalled", "label": "omp-caller:friday", "summary": "stalled"}, coordinator_subscriber_id="jarvis-omp-events")
        dispatcher._queue.join()
        routed = sorted((json.loads(body)["summary"], path) for (body, _), path in zip(Receiver.received, Receiver.paths))
        self.assertEqual(routed, [("omp-caller:friday", "/friday"), ("omp-caller:gone", "/jarvis"),
                                  ("omp-caller:karen", "/jarvis"), ("stalled", "/friday")])
        self.assertEqual(list_config(self.root)["caller_routes"]["omp-caller:friday"], "friday-omp-events")

    def test_only_allowlisted_events_post_and_the_rest_are_logged_locally(self):
        add_subscriber(self.root, self.subscriber(mode="conversation", auth_header="X-Token", auth_value="synthetic-secret-value"))
        add_subscriber(self.root, self.subscriber(id="observer", url=self.url.replace("/hook", "/observer")))
        dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(dispatcher.close)
        for event in ("run.deadline", "lane.idle", "daemon.up", "disk.low"):
            dispatcher.emit({"event": event, "job_id": "j1", "summary": "leak synthetic-secret-value"}, coordinator_subscriber_id="test")
        dispatcher.emit({"event": "run.done", "job_id": "j2", "summary": "ownerless"})
        for event in ("run.done", "run.error", "run.needs_input", "run.stalled"):
            dispatcher.emit({"event": event, "job_id": "j3"}, coordinator_subscriber_id="test")
        dispatcher._queue.join()
        self.assertCountEqual([json.loads(body)["event"] for body, _ in Receiver.received],
                              ["run.done", "run.error", "run.needs_input", "run.stalled", "run.stalled"])
        logged = self.filtered()
        self.assertEqual([(e["event"], e["reason"]) for e in logged],
                         [("run.deadline", "not_allowlisted"), ("lane.idle", "not_allowlisted"), ("daemon.up", "not_allowlisted"),
                          ("disk.low", "not_allowlisted"), ("run.done", "no_route")])
        self.assertNotIn("synthetic-secret-value", (self.root / "logs" / "events-filtered.jsonl").read_text())
        self.assertEqual((self.root / "logs" / "events-filtered.jsonl").stat().st_mode & 0o777, 0o600)

    def test_filtered_log_is_size_capped(self):
        add_subscriber(self.root, self.subscriber())
        dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(dispatcher.close)
        with patch("bot_omp_delegate.webhooks.FILTERED_LOG_MAX_BYTES", 2000):
            for index in range(100):
                dispatcher.emit({"event": "lane.idle", "summary": f"idle {index} " + "x" * 50})
        current = self.root / "logs" / "events-filtered.jsonl"
        rotated = self.root / "logs" / "events-filtered.jsonl.1"
        self.assertLess(current.stat().st_size, 2500)
        self.assertLess(rotated.stat().st_size, 2500)
        self.assertIn("idle 99 ", current.read_text())
        self.assertEqual(Receiver.received, [])

    def test_route_config_is_validated(self):
        add_subscriber(self.root, self.subscriber())
        for keys, ok in (({"caller_routes": {"omp-caller:karen": "karen-omp-events"}}, True),
                         ({"caller_routes": {"omp-caller:karen": "https://x.invalid/hook"}}, False),
                         ({"caller_routes": ["omp-caller:karen"]}, False),
                         ({"needs_input_grace_seconds": 30}, False),
                         ({"tool_preapprovals": []}, False)):
            self.with_config(**keys)
            if ok:
                load_config(self.root)
            else:
                with self.assertRaises(ConfigError, msg=keys):
                    load_config(self.root)
            path = self.root / "webhooks.json"
            raw = json.loads(path.read_text())
            for key in ("caller_routes", "needs_input_grace_seconds", "tool_preapprovals"):
                raw.pop(key, None)
            path.write_text(json.dumps(raw)); os.chmod(path, 0o600)

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
        dispatcher.emit({"event": "run.done", "cwd": str(project), "summary": "x" * 290 + secret, "label": "OPS-42"}, coordinator_subscriber_id="test")
        dispatcher._queue.join()
        body, headers = Receiver.received[0]
        payload = json.loads(body)
        self.assertEqual(payload["summary"], "x" * 290 + "[redacted]")
        self.assertEqual((payload["version"], payload["branch"], payload["last_commit_sha"], payload["label"]),
                         (1, "event-check", expected_sha, "OPS-42"))
        self.assertEqual(headers["X-Token"], secret)

    def test_nested_input_and_long_message_redacted_but_intact(self):
        secret = "synthetic-credential-value-for-redaction"
        add_subscriber(self.root, self.subscriber(auth_header="X-Token", auth_value=secret))
        dispatcher = WebhookDispatcher(self.root, retry_delays=())
        self.addCleanup(dispatcher.close)
        dispatcher.emit({"event": "run.needs_input", "summary": "x" * 290 + secret,
                         "message": "y" * 400 + secret,
                         "input": {"input_id": "input-9",
                                   "request": {"question": "Approve " + secret + "?",
                                               "options": [secret, "ok"]}}},
                        coordinator_subscriber_id="test")
        dispatcher._queue.join()
        payload = json.loads(Receiver.received[0][0])
        self.assertEqual((payload["summary"], len(payload["summary"])), ("x" * 290 + "[redacted]", 300))
        self.assertEqual(payload["message"], "y" * 400 + "[redacted]")
        self.assertEqual(payload["input"]["input_id"], "input-9")
        self.assertEqual(payload["input"]["request"]["question"], "Approve [redacted]?")
        self.assertEqual(payload["input"]["request"]["options"], ["[redacted]", "ok"])

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
        dispatcher.emit({"event": "run.stalled"})
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
                dispatcher.emit({"event": "run.stalled"})
                self.assertTrue(entered.wait(1))
                dispatcher.emit({"event": "run.stalled"})
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
            summary = json.loads(body)["summary"]
            if summary == "first":
                entered.set()
                release.wait(2)
            delivered.append(summary)
            return True, 204, None
        try:
            with patch("bot_omp_delegate.webhooks._request", side_effect=request):
                dispatcher.emit({"event": "run.stalled", "summary": "first"})
                self.assertTrue(entered.wait(1))
                dispatcher.emit({"event": "run.stalled", "summary": "second"})
                timer = threading.Timer(.05, release.set)
                timer.start()
                before = time.monotonic()
                dispatcher.close(drain_timeout=.5)
                timer.join()
                self.assertLess(time.monotonic() - before, 1.5)
                self.assertEqual(delivered, ["first", "second"])
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
