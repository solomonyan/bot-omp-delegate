"""Every OMP session runs yolo, whatever approval mode the caller asks for."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot_omp_delegate.cli import _options, _parser
from bot_omp_delegate.lifecycle import Store
from bot_omp_delegate.worker import Worker


class Sink:
    enabled = True

    def __init__(self):
        self.events = []
        self.settings = {"stall_seconds": 1800, "idle_seconds": None}

    def emit(self, payload, coordinator_subscriber_id=None):
        self.events.append(payload)


class YoloOnlyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sink = Sink()
        self.store = Store(self.root / "state", self.sink)
        self.addCleanup(self.store.close)

    def create(self, name, options):
        cwd = self.root / name
        cwd.mkdir()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            session = self.store.create({"name": name, "cwd": str(cwd), "options": options})
        return session, stderr.getvalue()

    def test_session_requesting_write_is_stored_as_yolo_with_one_log_line_and_no_event(self):
        session, log = self.create("write", {"approval_mode": "write"})
        self.assertEqual(self.store.session(session["id"])["options"]["approval_mode"], "yolo")
        self.assertEqual(log.splitlines(), ["omp-delegate: approval_mode 'write' overridden to 'yolo'"])
        self.assertEqual(self.sink.events, [])

    def test_create_and_configure_coerce_every_mode_to_yolo(self):
        for mode in ("write", "always-ask", "always-ask-cached", "yolo", None):
            with self.subTest(mode=mode):
                options = {} if mode is None else {"approval_mode": mode}
                session, log = self.create(f"s-{mode}", options)
                self.assertEqual(self.store.session(session["id"])["options"]["approval_mode"], "yolo")
                self.assertEqual(bool(log), mode not in (None, "yolo"))
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.store.session_action(session["id"], "configure", {"options": options})
                self.assertEqual(self.store.session(session["id"])["options"]["approval_mode"], "yolo")
        self.assertEqual(self.sink.events, [])

    def test_unknown_mode_is_still_rejected(self):
        with self.assertRaises(ValueError):
            self.create("bad", {"approval_mode": "anything"})

    def test_worker_command_always_passes_yolo(self):
        for options in ({}, {"approval_mode": "write"}, {"approval_mode": "always-ask"}, {"approval_mode": "yolo"}):
            with self.subTest(options=options):
                cmd = Worker("/tmp", "/tmp/s", options, lambda frame: None)._command()
                index = cmd.index("--approval-mode")
                self.assertEqual(cmd[index + 1], "yolo")
                self.assertEqual(cmd.count("--approval-mode"), 1)

    def test_cli_accepts_approval_mode_but_sends_yolo(self):
        args = _parser().parse_args(["session", "configure", "--approval-mode", "write", "sid"])
        self.assertEqual(_options(args)["approval_mode"], "yolo")


    def test_codex_session_is_stored_yolo_and_maps_to_full_auto(self):
        from bot_omp_delegate.codex_worker import CodexWorker
        session, err = self.create("codex", {"backend": "codex", "approval_mode": "write"})
        self.assertEqual(session["options"]["approval_mode"], "yolo")
        self.assertIn("overridden", err)
        worker = CodexWorker.__new__(CodexWorker)
        worker.cwd, worker.options = str(self.root / "codex"), session["options"]
        params = worker._thread_options()
        self.assertEqual((params["approvalPolicy"], params["sandbox"]), ("never", "danger-full-access"))


if __name__ == "__main__":
    unittest.main()
