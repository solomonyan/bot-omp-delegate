"""Authenticated loopback transport and durable delegation state."""
from __future__ import annotations

import copy
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import uuid

from .worker import Worker

TERMINAL = {"completed", "aborted", "error", "interrupted"}
INPUT_METHODS = {"select", "confirm", "input", "editor", "ask"}


class Conflict(ValueError):
    pass


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        os.chmod(tmp, 0o600)
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


class Store:
    def __init__(self, root: Path):
        self.root = root
        private_dir(root)
        private_dir(root / "jobs")
        private_dir(root / "sessions")
        self.cv = threading.Condition(threading.RLock())
        self.workers: dict[str, Worker] = {}
        self.starting: set[str] = set()
        self.closing = False
        self.sessions = self._load(root / "sessions.json", {})
        self.jobs: dict[str, dict] = {}
        for path in (root / "jobs").glob("*.json"):
            job = self._load(path, {})
            if job["status"] not in TERMINAL:
                job.update(status="interrupted", error="Daemon stopped before the job settled; prompt was not replayed", finishedAt=time.time())
                atomic_json(path, job)
            self.jobs[job["id"]] = job
        for session in self.sessions.values():
            session["activeJob"] = None
        self._save_sessions()

    @staticmethod
    def _load(path: Path, default):
        if not path.exists():
            return default
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)

    def _save_sessions(self):
        atomic_json(self.root / "sessions.json", self.sessions)

    def _save_job(self, job):
        atomic_json(self.root / "jobs" / f'{job["id"]}.json', job)

    def session(self, sid):
        if sid not in self.sessions:
            raise KeyError("Unknown session")
        return self.sessions[sid]

    def job(self, jid):
        if jid not in self.jobs:
            raise KeyError("Unknown job")
        return self.jobs[jid]

    def create(self, data):
        name = text(data, "name")
        cwd = str(Path(text(data, "cwd")).expanduser().resolve(strict=True))
        if not Path(cwd).is_dir():
            raise ValueError("cwd must be a directory")
        options = validate_options(data.get("options", {}))
        with self.cv:
            if any(s["name"] == name for s in self.sessions.values()):
                raise Conflict("Session name already exists; reuse its id")
            sid = uuid.uuid4().hex
            session_dir = self.root / "sessions" / sid
            private_dir(session_dir)
            session = dict(id=sid, name=name, cwd=cwd, options=options, activeJob=None, sessionDir=str(session_dir))
            self.sessions[sid] = session
            self._save_sessions()
            return copy.deepcopy(session)

    def _available(self, session):
        if self.closing:
            raise Conflict("Daemon is shutting down")
        for other in self.sessions.values():
            if other["cwd"] == session["cwd"] and (other["activeJob"] or other["id"] in self.starting):
                raise Conflict("Working directory is busy; use a separate worktree for parallel jobs")

    def _worker(self, sid):
        # Called only while this session owns a job or a compaction reservation.
        with self.cv:
            existing = self.workers.get(sid)
            session = copy.deepcopy(self.session(sid))
        if existing:
            return existing
        worker = Worker(session["cwd"], session["sessionDir"], session["options"], lambda frame: self.event(sid, frame))
        try:
            worker.start()
            worker.request("set_session_name", name=session["name"])
            worker.request("set_ask_dialog", enabled=True)
        except Exception:
            worker.close()
            raise
        with self.cv:
            if self.closing:
                worker.close()
                raise RuntimeError("Daemon shutting down")
            self.workers[sid] = worker
        return worker

    def turn(self, sid, message):
        with self.cv:
            session = self.session(sid)
            self._available(session)
            jid = uuid.uuid4().hex
            job = dict(id=jid, sessionId=sid, status="running", error=None, finalMessage="", pendingInput=[], startedAt=time.time(), finishedAt=None, nextSeq=0)
            self.jobs[jid] = job
            session["activeJob"] = jid
            self._save_job(job)
            self._save_sessions()
            initial = copy.deepcopy(job)
            threading.Thread(target=self._run, args=(sid, jid, message), daemon=True).start()
            return initial

    def _run(self, sid, jid, message):
        try:
            worker = self._worker(sid)
            with self.cv:
                if self.job(jid)["status"] in TERMINAL:
                    return
                worker.send_prompt(message, jid)
        except Exception as exc:
            with self.cv:
                self._finish(self.job(jid), "error", str(exc))

    def _finish(self, job, status, error=None):
        if job["status"] in TERMINAL:
            return
        job.update(status=status, error=error, pendingInput=[], finishedAt=time.time())
        session = self.session(job["sessionId"])
        if session["activeJob"] == job["id"]:
            session["activeJob"] = None
        self._save_job(job)
        self._save_sessions()
        self.cv.notify_all()

    def event(self, sid, frame):
        with self.cv:
            session = self.session(sid)
            if frame.get("type") == "delegate_worker_exit":
                self.workers.pop(sid, None)
            jid = session["activeJob"]
            if not jid:
                return
            job = self.job(jid)
            event = dict(frame, seq=job["nextSeq"])
            job["nextSeq"] += 1
            path = self.root / "jobs" / f"{jid}.jsonl"
            with path.open("a", encoding="utf-8") as stream:
                os.chmod(path, 0o600)
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            kind = frame.get("type")
            changed = False
            if kind == "extension_ui_request":
                if frame.get("method") in INPUT_METHODS:
                    job["pendingInput"].append(frame)
                    job["status"] = "waiting_input"
                    changed = True
                elif frame.get("method") == "cancel":
                    job["pendingInput"] = [p for p in job["pendingInput"] if p["id"] != frame.get("targetId")]
                    job["status"] = "waiting_input" if job["pendingInput"] else "running"
                    changed = True
            elif kind == "message_end":
                message = frame.get("message", {})
                if message.get("role") == "assistant":
                    job["finalMessage"] = "\n".join(part.get("text", "") for part in message.get("content", []) if part.get("type") == "text")
                    changed = True
            elif kind == "prompt_result" and frame.get("id") == jid:
                job["resultStatus"] = frame.get("status", "error")
                job["resultError"] = frame.get("error")
                if frame.get("sessionSettled"):
                    self._result(job)
                changed = True
            elif kind == "response" and frame.get("id") == jid:
                if not frame.get("success"):
                    self._finish(job, "error", str(frame.get("error", "Prompt rejected")))
                elif frame.get("data", {}).get("agentInvoked") is False:
                    self._finish(job, "completed")
            elif kind == "session_settled":
                # Only the prompt's yield result can complete this job, not startup quiescence.
                if "resultStatus" in job:
                    self._result(job)
            elif kind == "delegate_worker_exit":
                self._finish(job, "error", frame.get("error", "OMP exited"))
            if changed:
                self._save_job(job)
            self.cv.notify_all()

    def _result(self, job):
        error = job.get("resultError")
        self._finish(job, job["resultStatus"], error.get("message") if isinstance(error, dict) else error)

    def control(self, jid, action, data):
        with self.cv:
            job = self.job(jid)
            if job["status"] in TERMINAL:
                raise Conflict("Job is already terminal")
            worker = self.workers.get(job["sessionId"])
            if not worker:
                raise Conflict("OMP worker is still starting")
            if action == "answer":
                ui_id = text(data, "id")
                if not any(p["id"] == ui_id for p in job["pendingInput"]):
                    raise Conflict("UI request is no longer pending")
                allowed = {"id", "value", "confirmed", "answers", "cancelled"}
                if set(data) - allowed or not (set(data) - {"id"}):
                    raise ValueError("Supply a UI response: value, confirmed, answers, or cancelled")
                worker.respond_ui(data)
                job["pendingInput"] = [p for p in job["pendingInput"] if p["id"] != ui_id]
                job["status"] = "waiting_input" if job["pendingInput"] else "running"
                self._save_job(job)
                self.cv.notify_all()
                return copy.deepcopy(job)
        # Never hold the state lock while waiting for the RPC reader callback.
        if action == "steer":
            worker.request("steer", message=text(data, "message"))
        elif action == "interrupt":
            worker.request("abort_and_restore_queue")
        else:
            raise KeyError("Unknown action")
        with self.cv:
            return copy.deepcopy(self.job(jid))

    def compact(self, sid):
        with self.cv:
            self._available(self.session(sid))
            self.starting.add(sid)
        try:
            return self._worker(sid).request("compact")
        finally:
            with self.cv:
                self.starting.discard(sid)

    def events(self, jid, after):
        with self.cv:
            self.job(jid)
            path = self.root / "jobs" / f"{jid}.jsonl"
            result = []
            if path.exists():
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        event = json.loads(line)
                        if event["seq"] >= after:
                            result.append(event)
                            if len(result) == 100:
                                break
            return dict(events=result, next=result[-1]["seq"] + 1 if result else after)

    def close(self):
        with self.cv:
            self.closing = True
            workers = list(self.workers.values())
        for worker in workers:
            worker.close()
        with self.cv:
            for job in self.jobs.values():
                if job["status"] not in TERMINAL:
                    self._finish(job, "interrupted", "Daemon shutdown; prompt was not replayed")


def text(data, field):
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def validate_options(options):
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    allowed = {"model", "thinking", "profile", "config", "add_dir", "tools", "approval_mode"}
    if set(options) - allowed:
        raise ValueError("Unknown worker option")
    for key, value in options.items():
        if key in {"config", "add_dir"}:
            if not isinstance(value, list) or any(not isinstance(v, str) or not Path(v).is_absolute() or not Path(v).exists() for v in value):
                raise ValueError(f"{key} must contain existing absolute paths")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
    if options.get("approval_mode", "always-ask") not in {"always-ask", "always-ask-cached", "write", "yolo"}:
        raise ValueError("Unknown approval_mode")
    return options


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # Request paths and bodies can contain private project information.

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_GET(self):
        self.route(False)

    def do_POST(self):
        self.route(True)

    def route(self, post):
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, "Bearer " + self.server.token):
            self.reply(401, {"error": "Unauthorized"})
            return
        # Reject browser-origin requests, even if a local page obtains a token.
        if self.headers.get("Origin"):
            self.reply(403, {"error": "Browser origins are not accepted"})
            return
        store = self.server.store
        parsed = urlsplit(self.path)
        parts = parsed.path.strip("/").split("/")
        try:
            data = {}
            if post:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1048576:
                    raise ValueError("Request body exceeds 1 MiB")
                self.connection.settimeout(15)
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("Request must be an object")
            if parts == ["health"] and not post:
                result = {"status": "ok"}
            elif parts == ["sessions"]:
                if post:
                    result = store.create(data)
                else:
                    with store.cv:
                        result = {"sessions": copy.deepcopy(list(store.sessions.values()))}
            elif len(parts) == 3 and parts[0] == "sessions" and post:
                if parts[2] == "turn":
                    result = store.turn(parts[1], text(data, "message"))
                elif parts[2] == "compact":
                    result = store.compact(parts[1])
                else:
                    raise KeyError("Unknown route")
            elif len(parts) in {2, 3} and parts[0] == "jobs":
                jid = parts[1]
                if len(parts) == 2 and not post:
                    with store.cv:
                        result = copy.deepcopy(store.job(jid))
                elif len(parts) == 3 and not post and parts[2] == "wait":
                    timeout = float(parse_qs(parsed.query).get("timeout", ["60"])[0])
                    if not math.isfinite(timeout) or not 0 <= timeout <= 60:
                        raise ValueError("timeout must be between 0 and 60")
                    with store.cv:
                        initial = copy.deepcopy(store.job(jid))
                        store.cv.wait_for(lambda: store.job(jid) != initial or store.job(jid)["status"] in TERMINAL, timeout)
                        result = copy.deepcopy(store.job(jid))
                elif len(parts) == 3 and not post and parts[2] == "events":
                    after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                    if after < 0:
                        raise ValueError("after must be nonnegative")
                    result = store.events(jid, after)
                elif len(parts) == 3 and post:
                    result = store.control(jid, parts[2], data)
                else:
                    raise KeyError("Unknown route")
            elif parts == ["shutdown"] and post:
                result = {"status": "stopping"}
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                raise KeyError("Unknown route")
            self.reply(200, result)
        except Conflict as exc:
            self.reply(409, {"error": str(exc)})
        except KeyError as exc:
            self.reply(404, {"error": str(exc)})
        except (ValueError, OSError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception as exc:
            self.reply(500, {"error": str(exc)})


def serve(state_dir: Path, port: int) -> None:
    import fcntl
    root = state_dir.expanduser().resolve()
    private_dir(root)
    # A stable advisory lock prevents two daemons from owning the same saved jobs.
    with (root / "daemon.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("A daemon already owns this state directory") from exc
        token_path = root / "token"
        if not token_path.exists():
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32))
        token_path.chmod(0o600)
        token = token_path.read_text().strip()
        if len(token) < 32:
            raise RuntimeError("Invalid daemon token file")
        # Bind before loading state: an occupied port must not mutate saved jobs.
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = True
        try:
            server.token = token
            server.store = Store(root)
            def stop(_signum, _frame):
                threading.Thread(target=server.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            print(json.dumps({"status": "ready", "host": "127.0.0.1", "port": port}), flush=True)
            server.serve_forever()
        finally:
            if hasattr(server, "store"):
                server.store.close()
            server.server_close()
