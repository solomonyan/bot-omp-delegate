"""Resident Kimi Code ACP worker; native sessions remain Kimi-managed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import uuid

from .worker import NoActiveTurn, PendingRpc, Worker, _FrameDecoder


class _Cancellation:
    """Cancellation is a notification; only the owning prompt response settles it."""

    def __init__(self, worker, prompt_id):
        self.worker, self.prompt_id = worker, prompt_id

    def result(self, timeout=120):
        worker = self.worker
        with worker._state:
            if not worker._state.wait_for(
                lambda: worker._prompt_id != self.prompt_id or worker._closed.is_set() or not worker.alive,
                timeout=timeout,
            ):
                raise RuntimeError("Kimi did not settle after cancellation")
            if worker._prompt_id == self.prompt_id:
                raise RuntimeError("Kimi worker stopped before cancellation settled")


class KimiWorker(Worker):
    backend = "kimi"

    def __init__(self, cwd, session_dir, options, on_event):
        super().__init__(cwd, session_dir, options, on_event)
        self._state = threading.Condition(threading.RLock())
        self._identity_path = Path(session_dir) / "kimi-session.json"
        self._identity = self._load_identity()
        self.session_id = None
        self._prompt_id = None
        self._cancelling = False
        self._inputs = {}
        self._parts = []
        self._message_id = None

    def _load_identity(self):
        if self._identity_path.is_symlink():
            raise ValueError("Kimi session identity must not be a symbolic link")
        try:
            with self._identity_path.open(encoding="utf-8") as stream:
                identity = json.load(stream)
        except FileNotFoundError:
            return None
        if (not isinstance(identity, dict) or identity.get("version") != 1
                or not isinstance(identity.get("sessionId"), str) or not identity["sessionId"]):
            raise ValueError("Invalid saved Kimi session identity")
        if identity.get("cwd") != self.cwd or identity.get("additionalDirectories") != self.options.get("add_dir", []):
            raise ValueError("Saved Kimi workspace does not match this conversation")
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
        return [os.environ.get("OMP_DELEGATE_KIMI", "kimi"), "acp"]

    def start(self):
        try:
            self._spawn()
            initialized = self.request("initialize", timeout=30, protocolVersion=1,
                clientInfo={"name": "bot_omp_delegate", "version": "1"},
                clientCapabilities={"elicitation": {"form": {}}})
            if not isinstance(initialized, dict) or type(initialized.get("protocolVersion")) is not int or initialized["protocolVersion"] != 1:
                raise RuntimeError("Kimi does not support ACP protocol version 1")
            capabilities = initialized.get("agentCapabilities", {})
            params = {"cwd": self.cwd, "mcpServers": []}
            if self._identity is None:
                params["additionalDirectories"] = self.options.get("add_dir", [])
                response = self.request("session/new", timeout=30, **params)
                session_id = response.get("sessionId") if isinstance(response, dict) else None
                if not isinstance(session_id, str) or not session_id:
                    raise RuntimeError("Kimi returned no session identity")
            else:
                session_id = self._identity["sessionId"]
                session_capabilities = capabilities.get("sessionCapabilities", {})
                if session_capabilities.get("resume") is not None:
                    method = "session/resume"
                elif capabilities.get("loadSession") is True:
                    method = "session/load"
                else:
                    raise RuntimeError("Kimi cannot resume saved sessions")
                response = self.request(method, timeout=30, sessionId=session_id, **params)
                if isinstance(response, dict) and response.get("sessionId", session_id) != session_id:
                    raise RuntimeError("Kimi resumed a different session")
            # No active prompt exists during resume: replayed history is not
            # output of the new delegate job and must never wake its owner.
            self.session_id = session_id
            self._identity = {"version": 1, "sessionId": session_id, "cwd": self.cwd,
                              "additionalDirectories": list(self.options.get("add_dir", []))}
            self._save_identity()
            self.on_event({"type": "ready", "backend": "kimi", "sessionId": session_id})
        except Exception:
            self.close()
            raise

    def configure(self, name):
        """Apply native options; Kimi owns native titles, Store owns lane names."""
        for field in ("model", "thinking"):
            if self.options.get(field) is not None:
                self.request("session/set_config_option", sessionId=self.session_id,
                             configId=field, value=self.options[field])
        # Kimi delegation is full-auto. ACP ignores top-level CLI approval
        # flags, so confirm the native session setting on every startup.
        response = self.request("session/set_config_option", sessionId=self.session_id,
                                configId="mode", value="yolo")
        options = response.get("configOptions", []) if isinstance(response, dict) else []
        if not any(option.get("id") == "mode" and option.get("currentValue") == "yolo" for option in options):
            raise RuntimeError("Kimi did not confirm the required yolo mode")

    def begin_request(self, command, **payload):
        request_id = "kimi-" + uuid.uuid4().hex
        waiter = queue.Queue(maxsize=1)
        ticket = PendingRpc(self, request_id, waiter)
        with self._pending_lock:
            self._pending[request_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": command, "params": payload})
        except Exception:
            ticket.cancel()
            raise
        return ticket

    def send_prompt(self, message, prompt_id):
        with self._state:
            if self.session_id is None:
                raise RuntimeError("Kimi session is not initialized")
            if self._prompt_id is not None:
                raise RuntimeError("Kimi session is busy")
            self._prompt_id = prompt_id
            self._cancelling = False
            self._parts.clear()
            self._message_id = None
            try:
                self._send({"jsonrpc": "2.0", "id": prompt_id, "method": "session/prompt",
                            "params": {"sessionId": self.session_id, "prompt": [{"type": "text", "text": message}]}})
            except Exception:
                self._clear_prompt()
                raise

    def begin_control(self, action, **payload):
        if action == "steer":
            raise ValueError("Kimi ACP does not support in-turn steering; send a follow-up after settlement")
        if action != "interrupt":
            raise ValueError("Unknown Kimi control")
        with self._state:
            if self._prompt_id is None:
                raise NoActiveTurn("Kimi has no active delegate turn")
            prompt_id = self._prompt_id
            self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}})
            self._cancelling = True
            events = self._cancel_inputs()
        for event in events:
            self.on_event(event)
        return _Cancellation(self, prompt_id)

    def begin_agent(self, action, **payload):
        raise ValueError("Kimi ACP does not expose direct delegate subagent controls")

    def compact(self):
        # Current Kimi ACP advertises /compact but its native implementation
        # only returns an unavailable-service notice. Never report fake success.
        raise ValueError("Manual compaction is not available through Kimi ACP")

    def _flush_message(self):
        if not self._parts:
            return []
        text = "".join(self._parts)
        self._parts.clear()
        self._message_id = None
        return [{"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}]

    def _clear_prompt(self):
        self._prompt_id = None
        self._cancelling = False
        self._parts.clear()
        self._message_id = None
        self._inputs.clear()
        self._state.notify_all()

    @staticmethod
    def _cancel_response(request):
        result = {"outcome": {"outcome": "cancelled"}} if request["method"] == "session/request_permission" else {"action": "cancel"}
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    def _cancel_inputs(self):
        events = []
        for local_id, request in list(self._inputs.items()):
            self._send(self._cancel_response(request))
            self._inputs.pop(local_id)
            events.append({"type": "extension_ui_request", "method": "cancel", "targetId": local_id})
        return events

    def _finish_prompt(self, frame):
        events = self._flush_message()
        events.extend(self._cancel_inputs())
        error = frame.get("error")
        if error is not None:
            status = "error"
        else:
            result = frame.get("result")
            reason = result.get("stopReason") if isinstance(result, dict) else None
            if reason in {"end_turn", "refusal"}:
                status = "completed"
            elif reason == "cancelled":
                status = "interrupted"
            else:
                status, error = "error", {"message": f"Kimi stopped without completing the turn: {reason}"}
        event = {"type": "prompt_result", "id": self._prompt_id, "status": status, "sessionSettled": True}
        if error is not None:
            event["error"] = error
        events.append(event)
        self._clear_prompt()
        return events

    def _dispatch(self, frame):
        if frame.get("jsonrpc") != "2.0":
            raise RuntimeError("Invalid Kimi ACP JSON-RPC envelope")
        events = []
        with self._state:
            method = frame.get("method")
            if method is not None and "id" in frame:
                events.extend(self._input_events(frame))
            elif method == "session/update":
                params = frame.get("params", {})
                if self._prompt_id is not None and params.get("sessionId") == self.session_id:
                    events.extend(self._update_events(params.get("update", {})))
            elif method is None and "id" in frame:
                request_id = frame["id"]
                if self._prompt_id is not None and request_id == self._prompt_id:
                    events.extend(self._finish_prompt(frame))
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
        # Store takes cv -> _state. Releasing _state before callbacks avoids
        # reversing that order when native updates race with a control request.
        for event in events:
            self.on_event(event)

    def _update_events(self, update):
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") != "text":
                return []
            text = content.get("text", "")
            message_id = update.get("messageId")
            events = []
            if message_id is not None:
                if self._message_id is not None and message_id != self._message_id:
                    events.extend(self._flush_message())
                self._message_id = message_id
            self._parts.append(text)
            events.append({"type": "message_update", "assistantMessageEvent": {"delta": text}})
            return events
        if kind == "agent_thought_chunk":
            return [{"type": "message_update", "assistantMessageEvent": {"delta": update.get("content", {}).get("text", "")},
                     "reasoning": True}]
        if kind in {"tool_call", "tool_call_update"}:
            events = self._flush_message() if kind == "tool_call" else []
            content = [item["content"] for item in update.get("content", [])
                       if item.get("type") == "content" and isinstance(item.get("content"), dict)]
            terminal = update.get("status") in {"completed", "failed"}
            event = {"type": "tool_execution_end" if terminal else "tool_execution_update",
                     "toolCallId": update.get("toolCallId"), "update": update,
                     "result" if terminal else "partialResult": {"content": content}}
            events.append(event)
            return events
        return [{"type": "kimi_event", "update": update}]

    def _input_events(self, frame):
        method, params = frame["method"], frame.get("params", {})
        if method not in {"session/request_permission", "elicitation/create"}:
            self._send({"jsonrpc": "2.0", "id": frame["id"], "error": {"code": -32601, "message": "Unsupported Kimi client request: " + method}})
            return []
        request = {"id": frame["id"], "method": method, "params": params}
        if self._prompt_id is None or self._cancelling or params.get("sessionId") != self.session_id:
            self._send(self._cancel_response(request))
            return []
        # ACP ids can be strings or numbers; equal-looking values of different
        # JSON types must not resolve or deduplicate one another.
        if any(type(r["id"]) is type(frame["id"]) and r["id"] == frame["id"] for r in self._inputs.values()):
            return []
        local_id = "kimi-input-" + uuid.uuid4().hex
        if method == "session/request_permission":
            options = params.get("options")
            if (not isinstance(options, list) or not options
                    or any(not isinstance(o, dict) or not isinstance(o.get("optionId"), str)
                           or not o["optionId"] or not isinstance(o.get("name"), str) for o in options)
                    or len({o["optionId"] for o in options}) != len(options)):
                raise RuntimeError("Kimi returned invalid permission options")
            event = {"type": "extension_ui_request", "id": local_id, "method": "select",
                     "title": params.get("toolCall", {}).get("title", "Kimi requests a decision"),
                     "options": [{"value": o["optionId"], "label": o["name"], "kind": o.get("kind")} for o in options],
                     "request": params, "backend": "kimi"}
        else:
            try:
                if params.get("mode", "form") != "form":
                    raise ValueError("Only Kimi form elicitation is supported")
                fields, questions = self._form_fields(params.get("requestedSchema"))
            except ValueError as exc:
                self._send({"jsonrpc": "2.0", "id": frame["id"], "error": {"code": -32602, "message": str(exc)}})
                return []
            request["fields"] = fields
            event = {"type": "extension_ui_request", "id": local_id, "method": "ask",
                     "title": params.get("message", "Kimi requests input"), "questions": questions,
                     "request": params, "backend": "kimi"}
        self._inputs[local_id] = request
        return [event]

    @staticmethod
    def _form_fields(schema):
        if not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
            raise ValueError("Kimi form must describe object properties")
        properties = schema["properties"]
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) or key not in properties for key in required):
            raise ValueError("Invalid required Kimi form fields")
        fields, questions = {}, []
        for key, prop in properties.items():
            if not isinstance(prop, dict) or prop.get("type") not in {"string", "array"}:
                raise ValueError("Unsupported Kimi form field type")
            multiple = prop["type"] == "array"
            choice_schema = prop.get("items", {}) if multiple else prop
            if not isinstance(choice_schema, dict):
                raise ValueError("Invalid Kimi choice schema")
            alternatives = choice_schema.get("oneOf", choice_schema.get("anyOf"))
            choices = choice_schema.get("enum")
            if alternatives is not None:
                if not isinstance(alternatives, list) or any(not isinstance(o, dict) or "const" not in o for o in alternatives):
                    raise ValueError("Invalid Kimi form choices")
                choices = [o["const"] for o in alternatives]
            if choices is not None and (not isinstance(choices, list) or not choices
                    or any(not isinstance(value, str) for value in choices) or len(set(choices)) != len(choices)):
                raise ValueError("Kimi form choices must be distinct strings")
            if multiple and choices is None:
                raise ValueError("Kimi multi-select requires declared choices")
            minimum = prop.get("minItems", 0) if multiple else prop.get("minLength", 0)
            maximum = prop.get("maxItems") if multiple else prop.get("maxLength")
            if type(minimum) is not int or minimum < 0 or (maximum is not None and (type(maximum) is not int or maximum < minimum)):
                raise ValueError("Invalid Kimi form bounds")
            if any(constraint in prop for constraint in ("pattern", "$ref", "allOf", "not")):
                raise ValueError("Unsupported Kimi form constraint")
            fields[key] = {"multiple": multiple, "choices": choices, "required": key in required,
                           "minimum": minimum, "maximum": maximum}
            question = {"id": key, "question": prop.get("description") or prop.get("title") or key,
                        "header": prop.get("title", key), "multi": multiple, "required": key in required}
            if choices is not None:
                question["options"] = [{"label": value} for value in choices]
            questions.append(question)
        return fields, questions

    @staticmethod
    def _form_answer(fields, response):
        if "value" in response:
            if "answers" in response or not isinstance(response["value"], dict):
                raise ValueError("Supply a form value object or an answers list")
            content = dict(response["value"])
        else:
            answers = response.get("answers")
            if not isinstance(answers, list):
                raise ValueError("Kimi form answers must be a list")
            content = {}
            for answer in answers:
                if not isinstance(answer, dict) or set(answer) - {"id", "selectedOptions", "text"}:
                    raise ValueError("Invalid Kimi question answer")
                key = answer.get("id")
                if not isinstance(key, str) or key not in fields or key in content:
                    raise ValueError("Unknown or duplicate Kimi question ID")
                field = fields[key]
                if field["choices"] is not None:
                    selected = answer.get("selectedOptions")
                    if "text" in answer or not isinstance(selected, list) or (not field["multiple"] and len(selected) != 1):
                        raise ValueError("Select the exact Kimi question options")
                    content[key] = selected if field["multiple"] else selected[0]
                else:
                    if "selectedOptions" in answer or not isinstance(answer.get("text"), str):
                        raise ValueError("Supply text for this Kimi question")
                    content[key] = answer["text"]
        if any(key not in fields for key in content):
            raise ValueError("Unknown Kimi form field")
        for key, field in fields.items():
            if key not in content:
                if field["required"]:
                    raise ValueError("Answer every required Kimi question")
                continue
            value = content[key]
            if field["multiple"]:
                if not isinstance(value, list) or any(not isinstance(v, str) for v in value) or len(set(value)) != len(value):
                    raise ValueError("Kimi multi-select answers must be distinct strings")
                values = value
            else:
                if not isinstance(value, str):
                    raise ValueError("Kimi question answers must be strings")
                values = [value]
            if len(value) < field["minimum"] or (field["maximum"] is not None and len(value) > field["maximum"]):
                raise ValueError("Kimi answer is outside the requested bounds")
            if field["choices"] is not None and any(v not in field["choices"] for v in values):
                raise ValueError("Unknown Kimi answer option")
        return {"action": "accept", "content": content}

    def respond_ui(self, response):
        with self._state:
            request = self._inputs.get(response["id"])
            if request is None:
                raise ValueError("Kimi input request is no longer pending")
            if "cancelled" in response and type(response["cancelled"]) is not bool:
                raise ValueError("cancelled must be a boolean")
            if "confirmed" in response and type(response["confirmed"]) is not bool:
                raise ValueError("confirmed must be a boolean")
            if response.get("cancelled") is True:
                if set(response) - {"id", "cancelled"}:
                    raise ValueError("Cannot both answer and cancel Kimi input")
                frame = self._cancel_response(request)
            elif request["method"] == "elicitation/create":
                if set(response) - {"id", "answers", "value", "confirmed"}:
                    raise ValueError("Supply Kimi question answers or form values")
                if "confirmed" in response:
                    if response["confirmed"] is not False or set(response) != {"id", "confirmed"}:
                        raise ValueError("A Kimi form needs answers, not blanket confirmation")
                    result = {"action": "decline"}
                else:
                    result = self._form_answer(request["fields"], response)
                frame = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            else:
                if set(response) - {"id", "value", "confirmed"}:
                    raise ValueError("Supply an exact Kimi option ID or confirmation")
                options = request["params"]["options"]
                if "confirmed" in response:
                    if "value" in response:
                        raise ValueError("Cannot supply both a Kimi option and confirmation")
                    kind = "allow_once" if response["confirmed"] else "reject_once"
                    candidates = [o for o in options if o.get("kind") == kind]
                    if len(candidates) != 1:
                        raise ValueError("Select an exact Kimi option ID; confirmation is ambiguous")
                    selected = candidates[0]["optionId"]
                else:
                    selected = response.get("value")
                    if not isinstance(selected, str) or not any(o["optionId"] == selected for o in options):
                        raise ValueError("Unknown Kimi permission option")
                frame = {"jsonrpc": "2.0", "id": request["id"],
                         "result": {"outcome": {"outcome": "selected", "optionId": selected}}}
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
                    raise RuntimeError("Kimi ACP process exited")
                for frame in decoder.feed(data):
                    self._dispatch(frame)
        except Exception as exc:
            failure = RuntimeError(f"Kimi ACP reader failed: {exc}")
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
                    self._state.notify_all()
                self._emit_exit(failure)

    def close(self):
        try:
            super().close()
        finally:
            with self._state:
                self._state.notify_all()
