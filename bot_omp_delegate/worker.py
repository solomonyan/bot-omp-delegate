from __future__ import annotations

import base64
import binascii
import json
import os
import signal
import stat
import queue
import subprocess
import threading
import time
import uuid
from typing import Callable


class _FrameDecoder:
    """Strict JSONL and protocol-v2 chunk decoder."""

    def __init__(self, max_frame: int = 1_048_576, max_reassembled: int = 67_108_864):
        self.max_frame = max_frame
        self.max_reassembled = max_reassembled
        self.buffer = bytearray()
        self.chunks = None

    def feed(self, data: bytes):
        self.buffer.extend(data)
        frames = []
        while True:
            pos = self.buffer.find(b"\n")
            if pos < 0:
                if len(self.buffer) >= self.max_frame:
                    raise RuntimeError("RPC physical frame exceeds limit")
                break
            raw = bytes(self.buffer[:pos])
            del self.buffer[:pos + 1]
            if len(raw) + 1 > self.max_frame:
                raise RuntimeError("RPC physical frame exceeds limit")
            if not raw:
                continue
            try:
                frame = json.loads(raw.decode("utf-8", "strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Invalid RPC JSONL frame: {exc}") from exc
            if not isinstance(frame, dict):
                raise RuntimeError("RPC frame must be a JSON object")
            if frame.get("type") != "rpc_chunk":
                if self.chunks is not None:
                    raise RuntimeError("Interrupted RPC chunk sequence")
                frames.append(frame)
                continue
            frames.extend(self._chunk(frame))
        return frames

    def _chunk(self, frame):
        required = ("chunkId", "index", "count", "byteLength", "data")
        if any(key not in frame for key in required):
            raise RuntimeError("Malformed RPC chunk")
        cid, index, count, length = (frame["chunkId"], frame["index"], frame["count"], frame["byteLength"])
        if not isinstance(cid, str) or not cid or type(index) is not int or type(count) is not int or type(length) is not int:
            raise RuntimeError("Invalid RPC chunk metadata")
        if count <= 0 or index < 0 or index >= count or length < 0 or length > self.max_reassembled:
            raise RuntimeError("RPC chunk metadata exceeds limits")
        if self.chunks is None:
            if index != 0:
                raise RuntimeError("RPC chunk sequence does not start at zero")
            self.chunks = (cid, count, length, 0, bytearray())
        old_id, old_count, old_len, expected, data = self.chunks
        if cid != old_id or count != old_count or length != old_len or index != expected:
            raise RuntimeError("Interleaved or out-of-order RPC chunks")
        try:
            part = base64.b64decode(frame["data"], validate=True)
        except (binascii.Error, TypeError) as exc:
            raise RuntimeError("Invalid RPC chunk base64") from exc
        data.extend(part)
        if len(data) > length or len(data) > self.max_reassembled:
            raise RuntimeError("RPC reassembled frame exceeds limit")
        if expected + 1 < count:
            self.chunks = (cid, count, length, expected + 1, data)
            return []
        self.chunks = None
        if len(data) != length:
            raise RuntimeError("RPC chunk byteLength mismatch")
        try:
            result = json.loads(data.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid reassembled RPC frame: {exc}") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Reassembled RPC frame must be an object")
        return [result]

    def finish(self):
        if self.buffer or self.chunks is not None:
            raise RuntimeError("Truncated RPC output")


class PendingRpc:
    """A sent RPC command whose response can be awaited outside a state lock."""

    def __init__(self, worker, request_id, waiter):
        self.worker = worker
        self.request_id = request_id
        self.waiter = waiter

    def result(self, timeout=120):
        try:
            try:
                response = self.waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise RuntimeError("Timed out waiting for OMP RPC response") from exc
            if isinstance(response, Exception):
                raise RuntimeError(str(response)) from response
            if not response.get("success"):
                raise RuntimeError(str(response.get("error", "OMP RPC command failed")))
            return response.get("data")
        finally:
            self.cancel()

    def cancel(self):
        with self.worker._pending_lock:
            self.worker._pending.pop(self.request_id, None)


class Worker:
    def __init__(self, cwd: str, session_dir: str, options: dict, on_event: Callable[[dict], None]):
        self.cwd, self.session_dir, self.options, self.on_event = cwd, session_dir, dict(options or {}), on_event
        self.process = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._frames = queue.Queue(maxsize=1)
        self._closed = threading.Event()
        self._reader = None
        self._lifecycle_lock = threading.Lock()
        self._stderr_file = None

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self):
        return self.process.pid if self.alive else None

    def _command(self):
        cmd = [os.environ.get("OMP_DELEGATE_OMP", "omp"), "--mode", "rpc-ui", "--cwd", self.cwd, "--session-dir", self.session_dir, "--continue", "--no-title"]
        approval = self.options.get("approval_mode", "always-ask")
        if approval not in ("yolo", "write", "always-ask-cached", "always-ask"):
            raise ValueError("Invalid approval_mode")
        cmd += ["--approval-mode", approval]
        for key, flag in (("model", "--model"), ("thinking", "--thinking"), ("profile", "--profile"), ("tools", "--tools")):
            if self.options.get(key) is not None:
                cmd += [flag, str(self.options[key])]
        for value in self.options.get("config", []):
            cmd += ["--config", str(value)]
        for value in self.options.get("add_dir", []):
            cmd += ["--add-dir", str(value)]
        return cmd

    def start(self):
        with self._lifecycle_lock:
            if self._closed.is_set():
                raise RuntimeError("Worker was closed before startup")
            if self.process is not None:
                raise RuntimeError("Worker already started")
            os.makedirs(self.session_dir, exist_ok=True)
            log_path = os.path.join(self.session_dir, "worker.stderr.log")
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            self._stderr_file = os.fdopen(fd, "ab", buffering=0)
            try:
                self.process = subprocess.Popen(
                    self._command(), cwd=self.cwd, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=self._stderr_file, bufsize=0,
                    env=os.environ.copy(), start_new_session=True,
                )
            except OSError as exc:
                self._stderr_file.close()
                raise RuntimeError(f"Unable to start omp: {exc}") from exc
            self._reader = threading.Thread(target=self._read_stdout, name="omp-rpc-reader", daemon=True)
            self._reader.start()
        try:
            frame = self._frames.get(timeout=30)
        except queue.Empty as exc:
            self.close()
            raise RuntimeError("Timed out waiting for RPC ready frame") from exc
        if isinstance(frame, Exception):
            self.close()
            raise RuntimeError(str(frame)) from frame
        if not isinstance(frame, dict) or frame.get("type") != "ready":
            self.close()
            raise RuntimeError(f"Expected RPC ready frame, received {frame!r}")
        versions = frame.get("supportedProtocolVersions", [])
        if isinstance(versions, list) and 2 in versions:
            result = self.request("negotiate_protocol", protocolVersion=2, timeout=30)
            if not isinstance(result, dict) and result is not None:
                self.close()
                raise RuntimeError("Invalid protocol negotiation response")
        return frame

    def _read_stdout(self):
        decoder = _FrameDecoder()
        failure = None
        try:
            while True:
                data = self.process.stdout.read(65536)
                if not data:
                    decoder.finish()
                    failure = RuntimeError("OMP RPC process exited")
                    break
                for frame in decoder.feed(data):
                    self._dispatch(frame)
        except Exception as exc:
            failure = RuntimeError(f"OMP RPC reader failed: {exc}")
        finally:
            if not self._closed.is_set():
                try:
                    self._stop_process(self.process, graceful=False)
                except Exception as exc:
                    failure = RuntimeError(f"{failure}; worker cleanup failed: {exc}")
                self._fail_pending(failure or RuntimeError("OMP RPC process exited"))
                self._emit_exit(failure or RuntimeError("OMP RPC process exited"))

    def _fail_pending(self, error):
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for waiter in pending:
            try:
                waiter.put_nowait(error)
            except queue.Full:
                pass
        try:
            self._frames.put_nowait(error)
        except queue.Full:
            pass

    def _emit_exit(self, error):
        try:
            self.on_event({"type": "delegate_worker_exit", "error": str(error), "stillAlive": self.alive})
        except Exception:
            pass

    def _dispatch(self, frame):
        if frame.get("type") == "response" and isinstance(frame.get("id"), str):
            with self._pending_lock:
                waiter = self._pending.get(frame["id"])
            if waiter:
                try:
                    waiter.put_nowait(frame)
                except queue.Full:
                    pass
        if frame.get("type") == "ready":
            try:
                self._frames.put_nowait(frame)
            except queue.Full as exc:
                raise RuntimeError("Duplicate OMP ready frame") from exc
        try:
            self.on_event(frame)
        except Exception as exc:
            raise RuntimeError(f"Worker event callback failed: {exc}") from exc

    def _send(self, frame):
        if self._closed.is_set() or not self.alive:
            raise RuntimeError("OMP RPC worker is not running")
        raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > 1_048_576:
            raise ValueError("OMP input frame exceeds 1 MiB")
        with self._write_lock:
            try:
                remaining = memoryview(raw)
                while remaining:
                    written = self.process.stdin.write(remaining)
                    if not written:
                        raise RuntimeError("OMP stdin closed during command delivery")
                    remaining = remaining[written:]
                self.process.stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                raise RuntimeError(f"Unable to write to OMP RPC process: {exc}") from exc

    def begin_request(self, command: str, **payload):
        request_id = f"delegate-{uuid.uuid4().hex}"
        waiter = queue.Queue(maxsize=1)
        ticket = PendingRpc(self, request_id, waiter)
        with self._pending_lock:
            self._pending[request_id] = waiter
        try:
            self._send({"id": request_id, "type": command, **payload})
        except Exception:
            ticket.cancel()
            raise
        return ticket

    def request(self, command: str, timeout: float = 120, **payload):
        return self.begin_request(command, **payload).result(timeout)

    def send_prompt(self, message: str, prompt_id: str) -> None:
        self._send({"id": prompt_id, "type": "prompt", "message": message})

    def respond_ui(self, response: dict) -> None:
        self._send({"type": "extension_ui_response", **response})

    @staticmethod
    def _signal_group(process, sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def _stop_process(self, process, graceful):
        if process is None:
            return
        if graceful and process.poll() is None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)
                process.wait(timeout=2)
        # OMP may have exited before its same-group descendants. Detached jobs are
        # outside this process group and cannot be promised to stop.
        self._signal_group(process, signal.SIGTERM)
        time.sleep(0.05)
        self._signal_group(process, signal.SIGKILL)
        if process.poll() is None:
            raise RuntimeError("OMP process did not stop")

    def close(self):
        with self._lifecycle_lock:
            self._closed.set()
            process = self.process
        self._fail_pending(RuntimeError("OMP worker closed"))
        self._stop_process(process, graceful=True)
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)
            if self._reader.is_alive():
                raise RuntimeError("OMP event reader did not finish shutdown")
        if process is not None:
            process.stdin.close()
            process.stdout.close()
        if self._stderr_file:
            self._stderr_file.close()
