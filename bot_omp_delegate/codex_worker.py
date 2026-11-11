"""Persistent Codex app-server transport behind the resident worker interface."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid

from .worker import NoActiveTurn, PendingRpc, Worker, _FrameDecoder


class _TurnControl:
    """Capture the owning prompt before releasing Store's lifecycle lock."""

    def __init__(self, worker, prompt_id, action, payload):
        self.worker, self.prompt_id = worker, prompt_id
        self.action, self.payload = action, payload

    def result(self, timeout=120):
        worker = self.worker
        deadline = time.monotonic() + timeout
        with worker._state:
            if not worker._state.wait_for(
                lambda: worker._turn_id is not None or worker._prompt_id != self.prompt_id
                or worker._closed.is_set() or not worker.alive, timeout=timeout
            ):
                raise RuntimeError("Timed out waiting for Codex to start the turn")
            if worker._prompt_id != self.prompt_id:
                if self.action == "interrupt":
                    return None
                raise RuntimeError("Codex turn settled before steering")
            if worker._closed.is_set() or not worker.alive:
                raise RuntimeError("Codex worker is unavailable")
            if self.action == "interrupt":
                ticket = worker.begin_request("turn/interrupt", threadId=worker.thread_id, turnId=worker._turn_id)
            else:
                ticket = worker.begin_request("turn/steer", threadId=worker.thread_id,
                    expectedTurnId=worker._turn_id, input=[{"type": "text", "text": self.payload["message"]}])
        return ticket.result(timeout=max(0.01, deadline - time.monotonic()))


class CodexWorker(Worker):
    backend = "codex"

    def __init__(self, cwd, session_dir, options, on_event):
        super().__init__(cwd, session_dir, options, on_event)
        self._state = threading.Condition(threading.RLock())
        self._identity_path = Path(session_dir) / "codex-thread.json"
        self._identity = self._load_identity()
        self.thread_id = None
        self._prompt_id = self._turn_id = self._start_id = None
        self._early = []
        self._inputs = {}
        self._messages = set()
        self._compaction = None

    def _load_identity(self):
        if self._identity_path.is_symlink():
            raise ValueError("Codex thread identity must not be a symbolic link")
        try:
            with self._identity_path.open(encoding="utf-8") as stream:
                identity = json.load(stream)
        except FileNotFoundError:
            return None
        if (not isinstance(identity, dict) or identity.get("version") != 1
                or not isinstance(identity.get("threadId"), str) or not identity["threadId"]
                or type(identity.get("dispatchAttempted")) is not bool):
            raise ValueError("Invalid saved Codex thread identity")
        return identity

    def _save_identity(self):
        temporary = self._identity_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(self._identity, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._identity_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _command(self):
        command = [os.environ.get("OMP_DELEGATE_CODEX", "codex")]
        if self.options.get("profile"):
            raise ValueError("Codex app-server does not support named profiles")
        if self.options.get("add_dir"):
            command += ["--config", "sandbox_workspace_write.writable_roots=" + json.dumps(self.options["add_dir"])]
        return command + ["app-server", "--listen", "stdio://"]

    def _thread_options(self):
        params = {"cwd": self.cwd}
        if self.options.get("model"):
            params["model"] = self.options["model"]
        approval = self.options.get("approval_mode")
        if approval is not None:
            policy, sandbox = {
                "yolo": ("never", "danger-full-access"),
                "write": ("on-request", "workspace-write"),
                "always-ask-cached": ("on-request", "workspace-write"),
                "always-ask": ("untrusted", "workspace-write"),
            }[approval]
            params.update(approvalPolicy=policy, sandbox=sandbox)
        return params

    def start(self):
        try:
            self._spawn()
            self.request("initialize", timeout=30,
                clientInfo={"name": "bot_omp_delegate", "title": "Coding delegate", "version": "1"})
            self._send({"method": "initialized"})
            params = self._thread_options()
            # Empty prewarm-only threads have no native rollout. Any attempted
            # prompt, however, requires strict resumption without reset/replay.
            if self._identity is not None and self._identity["dispatchAttempted"]:
                result = self.request("thread/resume", timeout=30,
                    threadId=self._identity["threadId"], excludeTurns=True, **params)
            else:
                result = self.request("thread/start", timeout=30, **params)
            thread_id = result.get("thread", {}).get("id") if isinstance(result, dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise RuntimeError("Codex returned no thread identity")
            if self._identity is not None and self._identity["dispatchAttempted"] and thread_id != self._identity["threadId"]:
                raise RuntimeError("Codex resumed a different thread")
            attempted = bool(self._identity and self._identity["dispatchAttempted"])
            self.thread_id = thread_id
            self._identity = {"version": 1, "threadId": thread_id, "dispatchAttempted": attempted}
            self._save_identity()
            self.on_event({"type": "ready", "backend": "codex", "threadId": thread_id})
        except Exception:
            self.close()
            raise

    def configure(self, name):
        return self.request("thread/name/set", threadId=self.thread_id, name=name)

    def begin_request(self, command, **payload):
        request_id = "codex-" + uuid.uuid4().hex
        waiter = queue.Queue(maxsize=1)
        ticket = PendingRpc(self, request_id, waiter)
        with self._pending_lock:
            self._pending[request_id] = waiter
        try:
            self._send({"id": request_id, "method": command, "params": payload})
        except Exception:
            ticket.cancel()
            raise
        return ticket

    def send_prompt(self, message, prompt_id):
        with self._state:
            if self._prompt_id is not None or self._compaction is not None:
                raise RuntimeError("Codex thread is busy")
            if not self.thread_id:
                raise RuntimeError("Codex thread is not initialized")
            # Persist uncertainty before the first send: a crash must not erase
            # a conversation whose task may already have reached Codex.
            if not self._identity["dispatchAttempted"]:
                self._identity["dispatchAttempted"] = True
                self._save_identity()
            self._prompt_id = self._start_id = prompt_id
            self._turn_id = None
            self._messages.clear()
            self._early.clear()
            params = {"threadId": self.thread_id, "input": [{"type": "text", "text": message}]}
            if self.options.get("thinking"):
                params["effort"] = self.options["thinking"]
            try:
                self._send({"id": prompt_id, "method": "turn/start", "params": params})
            except Exception:
                self._clear_turn()
                raise

    def begin_control(self, action, **payload):
        if action not in {"steer", "interrupt"}:
            raise ValueError("Unknown Codex control")
        with self._state:
            if self._prompt_id is None:
                raise NoActiveTurn("Codex has no active delegate turn")
            return _TurnControl(self, self._prompt_id, action, payload)

    def begin_agent(self, action, **payload):
        raise ValueError("Delegate subagent controls are OMP-only; Codex manages its own native agents")

    def compact(self):
        operation = {"event": threading.Event(), "turnId": None, "error": None}
        with self._state:
            if self._prompt_id is not None or self._compaction is not None:
                raise ValueError("Codex thread is busy")
            self._compaction = operation
        try:
            self.request("thread/compact/start", threadId=self.thread_id)
            if not operation["event"].wait(120):
                raise RuntimeError("Codex compaction did not settle")
            if operation["error"]:
                raise RuntimeError(operation["error"])
            return {"status": "completed", "threadId": self.thread_id}
        except Exception as exc:
            # An accepted asynchronous operation may still own the worktree.
            self.close()
            self._emit_exit(exc)
            raise
        finally:
            with self._state:
                self._compaction = None

    def _clear_turn(self):
        self._prompt_id = self._turn_id = self._start_id = None
        self._inputs.clear()
        self._early.clear()
        self._messages.clear()
        self._state.notify_all()

    def _finish_turn(self, status, error=None):
        event = {"type": "prompt_result", "id": self._prompt_id, "status": status, "sessionSettled": True}
        if error is not None:
            event["error"] = error
        self._clear_turn()
        return event

    def _dispatch(self, frame):
        events = []
        method = frame.get("method")
        with self._state:
            if method is not None and "id" in frame:
                events.extend(self._input_events(frame))
            elif method is None and "id" in frame:
                request_id = frame["id"]
                if self._start_id is not None and request_id == self._start_id:
                    if "error" in frame:
                        events.append(self._finish_turn("error", frame["error"]))
                    else:
                        turn = frame.get("result", {}).get("turn", {})
                        if not isinstance(turn.get("id"), str):
                            raise RuntimeError("Codex returned no turn identity")
                        self._turn_id, self._start_id = turn["id"], None
                        early, self._early = self._early, []
                        self._state.notify_all()
                        for notification in early:
                            events.extend(self._input_events(notification) if "id" in notification
                                          else self._notification(notification))
                else:
                    with self._pending_lock:
                        waiter = self._pending.get(request_id)
                    if waiter is not None:
                        response = {"success": "error" not in frame, "data": frame.get("result")}
                        if "error" in frame:
                            error = frame["error"]
                            response["error"] = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                        try:
                            waiter.put_nowait(response)
                        except queue.Full:
                            pass
            elif method is not None:
                params = frame.get("params", {})
                if params.get("threadId") != self.thread_id:
                    return
                if self._start_id is not None:
                    if len(self._early) >= 256:
                        raise RuntimeError("Too many Codex events before turn acceptance")
                    self._early.append(frame)
                else:
                    events.extend(self._notification(frame))
        # Callbacks acquire Store's lifecycle lock; never invoke them under the
        # state lock because Store controls take these locks in reverse order.
        for event in events:
            self.on_event(event)

    def _notification(self, frame):
        method, params = frame["method"], frame.get("params", {})
        if self._compaction is not None:
            operation = self._compaction
            if method == "turn/started":
                operation["turnId"] = params["turn"]["id"]
            elif method == "turn/completed" and params.get("turn", {}).get("id") == operation["turnId"]:
                turn = params["turn"]
                if turn.get("status") != "completed":
                    operation["error"] = str(turn.get("error") or "Codex compaction failed")
                operation["event"].set()
            return []
        if self._prompt_id is None:
            return []
        if method == "serverRequest/resolved":
            request_id = params.get("requestId")
            for local_id, request in list(self._inputs.items()):
                if type(request["id"]) is type(request_id) and request["id"] == request_id:
                    self._inputs.pop(local_id)
                    return [{"type": "extension_ui_request", "method": "cancel", "targetId": local_id}]
            return []
        turn_id = params.get("turnId", params.get("turn", {}).get("id"))
        if turn_id != self._turn_id:
            return []
        if method == "item/agentMessage/delta":
            return [{"type": "message_update", "assistantMessageEvent": {"delta": params.get("delta", "")}}]
        if method in {"item/commandExecution/outputDelta", "item/fileChange/outputDelta"}:
            return [{"type": "tool_stream_update", "text": params.get("delta", "")}]
        if method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
            item = params["item"]
            if item["id"] in self._messages:
                return []
            self._messages.add(item["id"])
            return [{"type": "message_end", "message": {"role": "assistant",
                "content": [{"type": "text", "text": item.get("text", "")}]}}]
        if method == "turn/completed":
            turn = params["turn"]
            statuses = {"completed": "completed", "interrupted": "interrupted", "failed": "error"}
            status = statuses.get(turn.get("status"))
            if status is None:
                raise RuntimeError("Invalid Codex terminal turn status")
            return [self._finish_turn(status, turn.get("error"))]
        return [{"type": "codex_event", "method": method, "params": params}]

    def _input_events(self, frame):
        method, params = frame["method"], frame.get("params", {})
        if (params.get("threadId") != self.thread_id or self._prompt_id is None
                or (self._turn_id is not None and params.get("turnId") != self._turn_id)):
            self._send({"id": frame["id"], "error": {"code": -32000, "message": "No owning delegate turn"}})
            return []
        supported = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/tool/requestUserInput"}
        if method not in supported:
            self._send({"id": frame["id"], "error": {"code": -32601, "message": "Unsupported delegate request: " + method}})
            return []
        # Requests can race ahead of the response that identifies their turn.
        if self._start_id is not None:
            if len(self._early) >= 256:
                raise RuntimeError("Too many Codex events before turn acceptance")
            self._early.append(frame)
            return []
        if any(type(r["id"]) is type(frame["id"]) and r["id"] == frame["id"] for r in self._inputs.values()):
            return []
        local_id = "codex-input-" + uuid.uuid4().hex
        self._inputs[local_id] = {"id": frame["id"], "method": method, "params": params}
        if method == "item/tool/requestUserInput":
            return [{"type": "extension_ui_request", "id": local_id, "method": "ask", "questions": params["questions"]}]
        title = params.get("reason") or ("Allow command execution?" if "commandExecution" in method else "Allow file changes?")
        return [{"type": "extension_ui_request", "id": local_id, "method": "confirm", "title": title,
            "request": params, "backend": "codex"}]

    @staticmethod
    def _question_answers(questions, supplied):
        if not isinstance(supplied, list):
            raise ValueError("Codex answers must be a list of question answers")
        expected = {q["id"]: q for q in questions}
        answers = {}
        for answer in supplied:
            if not isinstance(answer, dict) or set(answer) - {"id", "selectedOptions", "text"}:
                raise ValueError("Invalid Codex question answer")
            question_id = answer.get("id")
            if question_id not in expected or question_id in answers:
                raise ValueError("Unknown or duplicate Codex question ID")
            question = expected[question_id]
            selected = answer.get("selectedOptions", [])
            if not isinstance(selected, list) or any(not isinstance(v, str) or not v for v in selected):
                raise ValueError("selectedOptions must contain answer strings")
            labels = {option["label"] for option in question.get("options") or []}
            if labels and any(v not in labels for v in selected):
                raise ValueError("Unknown Codex answer option")
            text = answer.get("text")
            if text is not None:
                if not isinstance(text, str) or not text.strip() or (labels and not question.get("isOther")):
                    raise ValueError("Free text is not permitted for this Codex question")
                selected = [*selected, text]
            if not selected:
                raise ValueError("Each Codex question requires an answer")
            answers[question_id] = {"answers": selected}
        if set(answers) != set(expected):
            raise ValueError("Answer every pending Codex question")
        return {"answers": answers}

    def respond_ui(self, response):
        with self._state:
            request = self._inputs.get(response["id"])
            if request is None:
                raise ValueError("Codex input request is no longer pending")
            cancelled = response.get("cancelled", False)
            if type(cancelled) is not bool:
                raise ValueError("cancelled must be a boolean")
            frame = {"id": request["id"]}
            if request["method"] == "item/tool/requestUserInput":
                if set(response) - {"id", "answers", "cancelled"}:
                    raise ValueError("Supply question answers or cancellation")
                if cancelled:
                    if "answers" in response:
                        raise ValueError("Cannot both answer and cancel a Codex question")
                    frame["error"] = {"code": -32000, "message": "Input cancelled by caller"}
                else:
                    frame["result"] = self._question_answers(request["params"]["questions"], response.get("answers"))
            else:
                if set(response) - {"id", "confirmed", "cancelled"}:
                    raise ValueError("Supply confirmed or cancelled for a Codex approval")
                if cancelled:
                    if "confirmed" in response:
                        raise ValueError("Cannot both confirm and cancel a Codex approval")
                    decision = "cancel"
                else:
                    if type(response.get("confirmed")) is not bool:
                        raise ValueError("confirmed must be a boolean")
                    decision = "accept" if response["confirmed"] else "decline"
                frame["result"] = {"decision": decision}
            self._send(frame)
            self._inputs.pop(response["id"], None)

    def _read_stdout(self):
        decoder = _FrameDecoder(max_frame=67_108_864)
        failure = None
        try:
            while True:
                data = self.process.stdout.read(65536)
                if not data:
                    decoder.finish()
                    raise RuntimeError("Codex app-server exited")
                for frame in decoder.feed(data):
                    self._dispatch(frame)
        except Exception as exc:
            failure = RuntimeError(f"Codex RPC reader failed: {exc}")
        finally:
            if not self._closed.is_set():
                try:
                    self._stop_process(self.process, graceful=False)
                except Exception as exc:
                    failure = RuntimeError(f"{failure}; worker cleanup failed: {exc}")
                if not self.alive:
                    self.process.stdin.close()
                    self.process.stdout.close()
                    if self._stderr_file is not None:
                        self._stderr_file.close()
                self._fail_pending(failure)
                with self._state:
                    if self._compaction is not None:
                        self._compaction["error"] = str(failure)
                        self._compaction["event"].set()
                    self._state.notify_all()
                self._emit_exit(failure)

    def close(self):
        with self._state:
            if self._compaction is not None:
                self._compaction["error"] = "Codex worker closed"
                self._compaction["event"].set()
        try:
            super().close()
        finally:
            with self._state:
                self._state.notify_all()
