import base64
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bot_omp_delegate.worker import Worker, _FrameDecoder


class DecoderTests(unittest.TestCase):
    def test_jsonl_split_across_reads_and_multiple_frames(self):
        decoder = _FrameDecoder()
        raw = b'{"type":"ready"}\n{"type":"response","success":true}\n'
        self.assertEqual(decoder.feed(raw[:7]), [])
        self.assertEqual(decoder.feed(raw[7:]), [{"type": "ready"}, {"type": "response", "success": True}])
        decoder.finish()

    def test_v2_chunk_boundaries_reassemble_unicode(self):
        logical = json.dumps({"type": "event", "text": "café"}, ensure_ascii=False).encode()
        pieces = [logical[:5], logical[5:]]
        decoder = _FrameDecoder()
        frames = []
        for i, piece in enumerate(pieces):
            frame = {"type": "rpc_chunk", "chunkId": "x", "index": i, "count": 2,
                     "byteLength": len(logical), "data": base64.b64encode(piece).decode()}
            frames += decoder.feed(json.dumps(frame).encode() + b"\n")
        self.assertEqual(frames, [{"type": "event", "text": "café"}])

    def test_rejects_interleaving_and_truncated_eof(self):
        decoder = _FrameDecoder()
        chunk = {"type": "rpc_chunk", "chunkId": "x", "index": 0, "count": 2,
                 "byteLength": 2, "data": "YQ=="}
        decoder.feed(json.dumps(chunk).encode() + b"\n")
        with self.assertRaises(RuntimeError):
            decoder.feed(b'{"type":"ready"}\n')
        with self.assertRaises(RuntimeError):
            decoder.finish()


class WorkerTests(unittest.TestCase):
    def child_worker(self, code, callback):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        worker = Worker(".", directory.name, {}, callback)
        worker.process = subprocess.Popen(
            [sys.executable, "-u", "-c", code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, start_new_session=True, bufsize=0,
        )
        self.addCleanup(worker.close)
        return worker

    def test_request_correlates_response_not_event_order(self):
        worker = Worker(".", "/tmp/session", {}, lambda event: None)
        class Fake:
            def poll(self): return None
            stdin = None
        worker.process = Fake()
        captured = {}
        def send(frame):
            captured.update(frame)
            worker._dispatch({"type": "agent_start"})
            worker._dispatch({"type": "response", "id": frame["id"], "success": True, "data": {"ok": 1}})
        worker._send = send
        self.assertEqual(worker.request("get_state"), {"ok": 1})
        self.assertEqual(captured["type"], "get_state")

    def test_pending_request_is_failed_and_exit_event_emitted_on_eof(self):
        events = []
        worker = self.child_worker("pass", events.append)
        waiter = __import__("queue").Queue(maxsize=1)
        worker._pending["id"] = waiter
        worker._read_stdout()
        self.assertIn("exited", str(waiter.get_nowait()))
        self.assertEqual(events[-1]["type"], "delegate_worker_exit")

    def test_callback_failure_fails_reader_and_emits_exit(self):
        events = []
        def callback(frame):
            events.append(frame)
            if frame.get("type") == "event":
                raise ValueError("callback")
        worker = self.child_worker(
            "import time; print('{\"type\":\"event\"}', flush=True); time.sleep(60)",
            callback,
        )
        worker._read_stdout()
        self.assertEqual(events[-1]["type"], "delegate_worker_exit")
        self.assertIsNotNone(worker.process.poll())

    def test_close_fails_pending_response_and_releases_worker(self):
        worker = self.child_worker("import time; time.sleep(60)", lambda frame: None)
        ticket = worker.begin_request("get_state")
        worker.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            ticket.result(timeout=0.1)
        self.assertFalse(worker.alive)

    def test_close_before_start_cannot_spawn_late_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(".", directory, {}, lambda frame: None)
            worker.close()
            with self.assertRaisesRegex(RuntimeError, "closed before startup"):
                worker.start()
            self.assertIsNone(worker.process)

    @patch("bot_omp_delegate.worker.subprocess.Popen", side_effect=OSError("missing"))
    def test_start_reports_spawn_failure(self, _popen):
        with tempfile.TemporaryDirectory() as session_dir:
            worker = Worker(".", session_dir, {}, lambda event: None)
            with self.assertRaisesRegex(RuntimeError, "Unable to start omp"):
                worker.start()

    def test_failed_response_raises_runtime_error(self):
        worker = Worker(".", "/tmp/session", {}, lambda event: None)
        worker.process = type("P", (), {"poll": lambda self: None})()
        worker.process.stdin = type("In", (), {"write": lambda *_: None, "flush": lambda *_: None})()
        worker._send = lambda frame: worker._dispatch({"type": "response", "id": frame["id"], "success": False, "error": "no"})
        with self.assertRaisesRegex(RuntimeError, "no"):
            worker.request("get_state")


if __name__ == "__main__":
    unittest.main()
