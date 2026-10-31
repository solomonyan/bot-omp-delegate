"""Durable lane/conversation state and serialized OMP worker lifecycles."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from .worker import Worker
from .notifications import Notifications

TERMINAL = {"completed", "aborted", "error", "interrupted"}
INPUT_METHODS = {"select", "confirm", "input", "editor", "ask"}
STATE_VERSION = 1


class Conflict(ValueError):
    pass


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Private state directories must not be symbolic links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


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
    return copy.deepcopy(options)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid state identifier")
    return value


class Store:
    def __init__(self, root: Path, webhooks=None, clock=None):
        self.root = Path(root).absolute()
        private_dir(self.root)
        self.root = self.root.resolve()
        private_dir(self.root / "jobs")
        private_dir(self.root / "sessions")
        self.cv = threading.Condition(threading.RLock())
        self.workers: dict[str, Worker] = {}
        self.ready: set[str] = set()
        self.startup_runs: set[str] = set()
        self.reserved: set[str] = set()
        self.lane_operations: set[str] = set()
        self.opening: set[str] = set()
        self.interrupting: set[str] = set()
        self.closing = False
        path = self.root / "state.json"
        if path.exists():
            state = self._load(path, {})
            if state.get("version") != STATE_VERSION:
                raise ValueError("Unsupported lifecycle state version")
            self.sessions = state["sessions"]
            self.lanes = state["lanes"]
        else:
            self.sessions = self._load(self.root / "sessions.json", {})
            self.lanes = {}
        if not isinstance(self.sessions, dict) or not isinstance(self.lanes, dict):
            raise ValueError("Invalid lifecycle metadata")
        for sid, session in self.sessions.items():
            if identifier(sid) != session["id"]:
                raise ValueError("Invalid saved session identity")
            session["activeJob"] = None
            session.setdefault("availability", "available")
            session.setdefault("lastActivity", time.time())
            session.setdefault("lastJob", None)
            session["worker"] = self._stopped()
        self.jobs = {}
        recovered_jobs = []
        for path in (self.root / "jobs").glob("*.json"):
            job = self._load(path, {})
            if identifier(job["id"]) != path.stem:
                raise ValueError("Invalid saved job identity")
            identifier(job["sessionId"])
            if job["status"] not in TERMINAL:
                job.update(status="interrupted", error="Daemon stopped before the job settled; prompt was not replayed", pendingInput=[], finishedAt=time.time())
                atomic_json(path, job)
                recovered_jobs.append(job)
            self.jobs[job["id"]] = job
        self._recover_purge()
        self._save_state()
        # Old metadata must not retain a second, undeletable copy of conversations.
        (self.root / "sessions.json").unlink(missing_ok=True)
        self.notifications = Notifications(webhooks, clock)
        self._monitor_stop = threading.Event()
        self._monitor_wake = threading.Event()
        self._monitor_thread = None
        for job in recovered_jobs:
            session = self.sessions.get(job["sessionId"])
            if session is not None:
                self.notifications.finished(job, session, self._lane_for_session(session["id"]))

    def _lane_for_session(self, sid):
        return next((lane for lane in self.lanes.values() if lane["sessionId"] == sid), None)

    def start_monitor(self):
        with self.cv:
            self._monitor_wake.set()
            if self.closing or not self.notifications.enabled:
                return
            if self._monitor_thread is None:
                self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="webhook-inactivity")
                self._monitor_thread.start()

    def _monitor_loop(self):
        while not self._monitor_stop.is_set():
            enabled = self.notifications.enabled
            if enabled:
                try:
                    self.monitor_tick()
                except Exception:
                    self.notifications.failures += 1
            self._monitor_wake.wait(1 if enabled else None)
            self._monitor_wake.clear()

    def monitor_tick(self):
        with self.cv:
            if not self.closing:
                self.notifications.tick(
                    self.jobs, self.sessions, self.lanes,
                    lambda sid: self._busy(sid) or self.session(sid)["availability"] != "available"
                    or self.session(sid)["worker"]["state"] in {"starting", "closing", "failed"}
                    or any(lid in self.lane_operations and lane["sessionId"] == sid for lid, lane in self.lanes.items()),
                )

    def deadline(self, jid):
        with self.cv:
            job = self.job(jid)
            if job["status"] not in TERMINAL and not job.get("deadlineNotified"):
                job["deadlineNotified"] = True
                self._save_job(job)
                session = self.session(job["sessionId"])
                self.notifications.emit("run.deadline", job, session, self._lane_for_session(session["id"]),
                                        "Coordinator waiter reached its deadline; the job may still be running")
            return copy.deepcopy(job)

    def request_shutdown(self, idle_only=False):
        if type(idle_only) is not bool:
            raise ValueError("idleOnly must be a boolean")
        with self.cv:
            if idle_only and (
                any(job["status"] not in TERMINAL for job in self.jobs.values())
                or self.reserved or self.startup_runs or self.opening or self.interrupting or self.lane_operations
                or any(s["worker"]["state"] in {"starting", "closing"} for s in self.sessions.values())
                or any(self.session(sid)["worker"]["state"] == "failed" and w.alive for sid, w in self.workers.items())
            ):
                raise Conflict("Daemon has active jobs or lifecycle transitions; restart was not performed")
            self.closing = True

    @staticmethod
    def _load(path, default):
        if not path.exists():
            return default
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)

    @staticmethod
    def _stopped():
        return {"state": "stopped", "pid": None, "generation": None, "error": None}

    def _save_state(self):
        atomic_json(self.root / "state.json", {"version": STATE_VERSION, "sessions": self.sessions, "lanes": self.lanes})

    def _save_job(self, job):
        atomic_json(self.root / "jobs" / f'{identifier(job["id"])}.json', job)

    def session(self, sid):
        if sid not in self.sessions:
            raise KeyError("Unknown session")
        return self.sessions[sid]

    def job(self, jid):
        if jid not in self.jobs:
            raise KeyError("Unknown job")
        return self.jobs[jid]

    def _accepting(self):
        if self.closing:
            raise Conflict("Daemon is shutting down")

    def _busy(self, sid):
        return bool(self.session(sid)["activeJob"] or sid in self.reserved or sid in self.startup_runs or sid in self.interrupting)

    def _check_cwd(self, cwd, excluding=()):
        for sid, session in self.sessions.items():
            if sid in excluding or session["cwd"] != cwd:
                continue
            worker = self.workers.get(sid)
            quarantined = worker is not None and worker.alive and session["worker"]["state"] in {"closing", "failed"}
            if self._busy(sid) or quarantined:
                raise Conflict("Working directory is busy; use a separate worktree for parallel jobs")

    def _available(self, session):
        self._accepting()
        if session["availability"] != "available":
            raise Conflict("Session is archived")
        sid = session["id"]
        if any(lid in self.lane_operations and lane["sessionId"] == sid for lid, lane in self.lanes.items()):
            raise Conflict("Lane lifecycle operation is in progress")
        if self._busy(sid):
            raise Conflict("Session is busy")
        worker = self.workers.get(sid)
        if worker is not None and (sid not in self.ready or not worker.alive or session["worker"]["state"] in {"failed", "closing"}):
            raise Conflict("Worker cleanup or startup is unresolved")
        self._check_cwd(session["cwd"], {sid})

    def _reserve(self, *sids):
        self._accepting()
        for sid in set(sids):
            if self._busy(sid):
                raise Conflict("Session is busy")
        for sid in set(sids):
            self._check_cwd(self.session(sid)["cwd"], set(sids))
        self.reserved.update(sids)

    def _release(self, *sids):
        with self.cv:
            self.reserved.difference_update(sids)
            self.cv.notify_all()

    def _new_session(self, name, cwd, options):
        sid = uuid.uuid4().hex
        directory = self.root / "sessions" / sid
        private_dir(directory)
        return {"id": sid, "name": name, "cwd": cwd, "options": copy.deepcopy(options),
                "activeJob": None, "lastJob": None, "sessionDir": str(directory),
                "availability": "available", "worker": self._stopped(), "lastActivity": time.time()}

    @staticmethod
    def _creation(data):
        name = text(data, "name")
        cwd = str(Path(text(data, "cwd")).expanduser().resolve(strict=True))
        if not Path(cwd).is_dir():
            raise ValueError("cwd must be a directory")
        return name, cwd, validate_options(data.get("options", {}))

    def create(self, data):
        name, cwd, options = self._creation(data)
        with self.cv:
            self._accepting()
            if any(s["name"] == name for s in self.sessions.values()):
                raise Conflict("Session name already exists; reuse its id")
            session = self._new_session(name, cwd, options)
            self.sessions[session["id"]] = session
            self._save_state()
            return self._snapshot_session(session)

    def _snapshot_session(self, session):
        result = copy.deepcopy(session)
        worker = self.workers.get(session["id"])
        result["worker"]["pid"] = worker.pid if worker is not None else None
        return result

    def list_sessions(self, include_archived=False):
        with self.cv:
            return {"sessions": [self._snapshot_session(s) for s in self.sessions.values()
                                 if include_archived or s["availability"] == "available"]}

    def show_session(self, sid):
        with self.cv:
            return self._snapshot_session(self.session(sid))

    def _worker(self, sid):
        with self.cv:
            session = self.session(sid)
            existing = self.workers.get(sid)
            if existing is not None:
                if sid in self.ready and existing.alive and session["worker"]["state"] in {"idle", "busy"}:
                    return existing
                raise Conflict("Worker is not ready")
            if self.closing or session["worker"]["state"] == "closing":
                raise Conflict("Worker startup cancelled")
            generation = uuid.uuid4().hex
            worker = Worker(session["cwd"], session["sessionDir"], session["options"],
                            lambda frame: self.event(sid, frame, generation))
            self.workers[sid] = worker
            session["worker"] = {"state": "starting", "pid": None, "generation": generation, "error": None}
            if session["activeJob"]:
                job = self.job(session["activeJob"])
                job["workerGeneration"] = generation
                self._save_job(job)
            self._save_state()
        try:
            worker.start()
            worker.request("set_session_name", name=session["name"])
            worker.request("set_ask_dialog", enabled=True)
            worker.request("set_subagent_subscription", level="events")
            with self.cv:
                if self.closing or self.workers.get(sid) is not worker or session["worker"]["state"] == "closing":
                    raise Conflict("Worker startup cancelled")
                self.ready.add(sid)
                session["worker"].update(state="busy" if session["activeJob"] else "idle", pid=worker.pid)
                self._save_state()
            return worker
        except Exception as exc:
            cleanup_error = None
            try:
                worker.close()
            except Exception as failure:
                cleanup_error = str(failure)
            with self.cv:
                if self.workers.get(sid) is worker:
                    if not worker.alive and cleanup_error is None:
                        self.workers.pop(sid)
                    self.ready.discard(sid)
                    cancelling = session["worker"]["state"] == "closing"
                    session["worker"].update(state="stopped" if cancelling and not worker.alive and cleanup_error is None else "failed",
                                             pid=worker.pid, error=cleanup_error or (None if cancelling else str(exc)))
                    self._save_state()
            raise

    def turn(self, sid, message, label=None):
        message = text({"message": message}, "message")
        if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in label)):
            raise ValueError("label must be non-empty, at most 200 characters and contain no control characters")
        with self.cv:
            session = self.session(sid)
            self._available(session)
            jid = uuid.uuid4().hex
            job = {"id": jid, "sessionId": sid, "status": "running", "error": None,
                   "finalMessage": "", "pendingInput": [], "startedAt": time.time(), "finishedAt": None,
                   "nextSeq": 0, "workerGeneration": session["worker"]["generation"]}
            if label is not None:
                job["label"] = label
            self.jobs[jid] = job
            session.update(activeJob=jid, lastJob=jid, lastActivity=time.time())
            session["worker"]["state"] = "busy" if sid in self.ready else "starting"
            self._save_job(job)
            self._save_state()
            self.notifications.started(job)
            initial = copy.deepcopy(job)
            threading.Thread(target=self._run, args=(sid, jid, message), daemon=True).start()
            return initial

    def _run(self, sid, jid, message):
        with self.cv:
            job = self.job(jid)
            session = self.session(sid)
            if job["status"] in TERMINAL or session["activeJob"] != jid:
                return
            self.startup_runs.add(sid)
        try:
            worker = self._worker(sid)
            with self.cv:
                if job.get("cancelRequested") or self.closing:
                    raise Conflict("Task cancelled before dispatch")
                if job["status"] not in TERMINAL and session["activeJob"] == jid:
                    job["workerGeneration"] = session["worker"]["generation"]
                    worker.send_prompt(message, jid)
                    self._save_job(job)
        except Exception as exc:
            with self.cv:
                if job["status"] not in TERMINAL:
                    self._finish(job, "aborted" if job.get("cancelRequested") else "error", str(exc))
        finally:
            with self.cv:
                self.startup_runs.discard(sid)
                self.cv.notify_all()

    def _finish(self, job, status, error=None):
        if job["status"] in TERMINAL:
            return
        job.update(status=status, error=error, pendingInput=[], finishedAt=time.time())
        session = self.session(job["sessionId"])
        if session["activeJob"] == job["id"]:
            session["activeJob"] = None
            if session["worker"]["state"] not in {"closing", "failed"}:
                session["worker"]["state"] = "idle" if job["sessionId"] in self.ready else "stopped"
        session["lastActivity"] = time.time()
        self._save_job(job)
        self._save_state()
        self.notifications.finished(job, session, self._lane_for_session(session["id"]))
        self.cv.notify_all()

    def event(self, sid, frame, generation=None):
        with self.cv:
            session = self.sessions.get(sid)
            if session is None or (generation is not None and generation != session["worker"]["generation"]):
                return
            kind = frame.get("type")
            if kind == "delegate_worker_exit":
                worker = self.workers.get(sid)
                alive = worker is not None and worker.alive
                if not alive:
                    self.workers.pop(sid, None)
                self.ready.discard(sid)
                session["worker"].update(state="failed", pid=worker.pid if alive else None, error=frame.get("error", "OMP exited"))
            elif kind == "ready":
                worker = self.workers.get(sid)
                session["worker"]["pid"] = worker.pid if worker is not None else None
            jid = session["activeJob"]
            if jid is None:
                if kind in {"delegate_worker_exit", "ready"}:
                    self._save_state()
                return
            job = self.job(jid)
            if generation is not None and generation != job["workerGeneration"]:
                return
            if self.notifications.has_output(frame):
                self.notifications.output(job)
            event = dict(frame, seq=job["nextSeq"])
            job["nextSeq"] += 1
            path = self.root / "jobs" / f"{jid}.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            changed = False
            if kind == "subagent_lifecycle":
                payload = frame.get("payload", {})
                child_id = payload.get("id")
                if payload.get("status") == "started" and isinstance(child_id, str):
                    job.setdefault("subagents", {})[child_id] = {
                        "parentToolCallId": payload.get("parentToolCallId"),
                        "sessionFile": payload.get("sessionFile"),
                    }
                    changed = True
            if kind == "extension_ui_request":
                if frame.get("method") in INPUT_METHODS:
                    job["pendingInput"].append(frame)
                elif frame.get("method") == "cancel":
                    job["pendingInput"] = [p for p in job["pendingInput"] if p["id"] != frame.get("targetId")]
                job["status"] = "waiting_input" if job["pendingInput"] else "running"
                changed = True
                if job["pendingInput"]:
                    self.notifications.needs_input(job, session, self._lane_for_session(sid))
                else:
                    self.notifications.resumed(job)
            elif kind == "message_end":
                message = frame.get("message", {})
                if message.get("role") == "assistant":
                    job["finalMessage"] = "\n".join(p.get("text", "") for p in message.get("content", []) if p.get("type") == "text")
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
                elif (frame.get("data") or {}).get("agentInvoked") is False:
                    self._finish(job, "completed")
            elif kind == "session_settled" and "resultStatus" in job:
                self._result(job)
            elif kind == "delegate_worker_exit":
                self._finish(job, "aborted" if job.get("cancelRequested") else "error", frame.get("error", "OMP exited"))
            if changed:
                self._save_job(job)
            self.cv.notify_all()

    def _result(self, job):
        error = job.get("resultError")
        self._finish(job, job["resultStatus"], error.get("message") if isinstance(error, dict) else error)

    def _job_worker(self, jid, mutation=True):
        self._accepting()
        job = self.job(jid)
        session = self.session(job["sessionId"])
        sid = session["id"]
        worker = self.workers.get(sid)
        if sid in self.reserved or sid in self.startup_runs or sid not in self.ready or worker is None or not worker.alive:
            raise Conflict("Worker is starting, closing or unavailable")
        if job["workerGeneration"] != session["worker"]["generation"] or session["lastJob"] != jid:
            raise Conflict("Job belongs to a stale worker or successor task")
        if mutation and (job["status"] in TERMINAL or session["activeJob"] != jid):
            raise Conflict("Job is already terminal")
        return job, session, worker

    def control(self, jid, action, data):
        with self.cv:
            job, session, worker = self._job_worker(jid)
            sid = session["id"]
            if action == "answer":
                ui_id = text(data, "id")
                if not any(p["id"] == ui_id for p in job["pendingInput"]):
                    raise Conflict("UI request is no longer pending")
                if set(data) - {"id", "value", "confirmed", "answers", "cancelled"} or not (set(data) - {"id"}):
                    raise ValueError("Supply value, confirmed, answers or cancelled")
                worker.respond_ui(data)
                job["pendingInput"] = [p for p in job["pendingInput"] if p["id"] != ui_id]
                job["status"] = "waiting_input" if job["pendingInput"] else "running"
                self.notifications.resumed(job)
                self._save_job(job)
                self.cv.notify_all()
                return copy.deepcopy(job)
            if action == "steer":
                command, payload = "steer", {"message": text(data, "message")}
            elif action == "interrupt":
                command, payload = "abort_and_restore_queue", {}
            else:
                raise KeyError("Unknown job action")
            self.reserved.add(sid)
            try:
                ticket = worker.begin_request(command, **payload)
            except Exception:
                self._release(sid)
                raise
        try:
            ticket.result()
            with self.cv:
                return copy.deepcopy(job)
        finally:
            self._release(sid)

    def agent_action(self, jid, action, data):
        with self.cv:
            job, session, worker = self._job_worker(jid, mutation=action in {"steer", "cancel"})
            if action == "list":
                command, payload = "get_subagents", {}
            elif action in {"read", "steer", "cancel"}:
                payload = {"subagentId": text(data, "subagentId")}
                if payload["subagentId"] not in job.get("subagents", {}):
                    raise Conflict("Subagent is not owned by this job")
                if action == "read":
                    offset = data.get("fromByte", 0)
                    if type(offset) is not int or offset < 0:
                        raise ValueError("fromByte must be a non-negative integer")
                    payload["fromByte"] = offset
                    command = "get_subagent_messages"
                elif action == "steer":
                    payload["message"] = text(data, "message")
                    command = "steer_subagent"
                else:
                    command = "cancel_subagent"
            else:
                raise KeyError("Unknown agent action")
            sid = session["id"]
            self.reserved.add(sid)
            try:
                ticket = worker.begin_request(command, **payload)
            except Exception:
                self._release(sid)
                raise
        try:
            result = ticket.result()
            if action == "list":
                if not isinstance(result, dict) or not isinstance(result.get("subagents"), list):
                    raise RuntimeError("Invalid OMP subagent registry response")
                result = dict(result, subagents=[child for child in result["subagents"]
                              if child.get("id") in job.get("subagents", {})])
            return {"jobId": jid, "workerGeneration": job["workerGeneration"], "result": result}
        finally:
            self._release(sid)

    def _stop_worker(self, sid):
        with self.cv:
            session = self.session(sid)
            worker = self.workers.get(sid)
            session["worker"]["state"] = "closing"
            self._save_state()
        if worker is not None:
            try:
                worker.close()
                if worker.alive:
                    raise RuntimeError("Worker remains alive after close")
            except Exception as exc:
                with self.cv:
                    session["worker"].update(state="failed", pid=worker.pid, error=str(exc))
                    self._save_state()
                raise
        with self.cv:
            self.workers.pop(sid, None)
            self.ready.discard(sid)
            session["worker"] = self._stopped()
            session["lastActivity"] = time.time()
            self._save_state()

    def _interrupt_close(self, sid):
        with self.cv:
            self._accepting()
            session = self.session(sid)
            if sid in self.interrupting or (sid in self.reserved and sid not in self.opening):
                raise Conflict("Lifecycle operation is in progress")
            self._check_cwd(session["cwd"], {sid})
            self.reserved.add(sid)
            self.interrupting.add(sid)
            deadline = time.monotonic() + 120
            jid = session["activeJob"]
            worker = self.workers.get(sid)
            startup = sid not in self.ready
            session["worker"]["state"] = "closing"
            if jid is not None:
                job = self.job(jid)
                job["cancelRequested"] = True
                self._save_job(job)
                if worker is None:
                    self._finish(job, "aborted", "Cancelled before worker startup")
            ticket = None
            if worker is not None and not startup and jid is not None:
                try:
                    ticket = worker.begin_request("abort_and_restore_queue")
                except Exception:
                    self.interrupting.discard(sid)
                    self._release(sid)
                    raise
        try:
            if startup and worker is not None:
                worker.close()
            if ticket is not None:
                ticket.result(timeout=max(0.1, deadline - time.monotonic()))
            with self.cv:
                while session["activeJob"] or sid in self.startup_runs or sid in self.opening:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Conflict("Worker did not settle after interruption")
                    self.cv.wait(remaining)
            self._stop_worker(sid)
            return self.show_session(sid)
        except Exception as exc:
            with self.cv:
                session["worker"].update(state="failed", error=str(exc))
                self._save_state()
            raise
        finally:
            with self.cv:
                self.interrupting.discard(sid)
            self._release(sid)

    def session_action(self, sid, action, data):
        if action == "close":
            interrupt = data.get("interrupt", False)
            if type(interrupt) is not bool:
                raise ValueError("interrupt must be a boolean")
            if interrupt:
                return self._interrupt_close(sid)
        if action not in {"open", "close", "archive", "unarchive", "configure", "delete"}:
            raise KeyError("Unknown session action")
        options = validate_options(data.get("options", {})) if action == "configure" else None
        with self.cv:
            session = self.session(sid)
            if action == "open" and session["availability"] != "available":
                raise Conflict("Session is archived")
            worker = self.workers.get(sid)
            if action == "open" and sid in self.ready and worker is not None and worker.alive and session["worker"]["state"] in {"idle", "busy"}:
                return self._snapshot_session(session)
            self._reserve(sid)
            if action == "open":
                self.opening.add(sid)
            try:
                if action == "delete":
                    return self._delete_session(session, data)
            except Exception:
                self._release(sid)
                raise
        try:
            if action == "open":
                self._worker(sid)
            elif action in {"close", "archive", "configure"}:
                self._stop_worker(sid)
            with self.cv:
                if action == "archive":
                    session["availability"] = "archived"
                elif action == "unarchive":
                    session["availability"] = "available"
                elif action == "configure":
                    session["options"].update(options)
                self._save_state()
                return self._snapshot_session(session)
        finally:
            with self.cv:
                self.opening.discard(sid)
            self._release(sid)

    def compact(self, sid):
        with self.cv:
            session = self.session(sid)
            if session["availability"] != "available":
                raise Conflict("Session is archived")
            self._reserve(sid)
        try:
            return self._worker(sid).request("compact")
        finally:
            self._release(sid)

    def create_lane(self, data):
        name, cwd, options = self._creation(data)
        with self.cv:
            self._accepting()
            if any(lane["name"] == name for lane in self.lanes.values()):
                raise Conflict("Lane name already exists")
            session = self._new_session(name, cwd, options)
            self.sessions[session["id"]] = session
            lid = uuid.uuid4().hex
            lane = {"id": lid, "name": name, "cwd": cwd, "sessionId": session["id"], "revision": 1, "history": [session["id"]]}
            self.lanes[lid] = lane
            self._save_state()
            return self._snapshot_lane(lane)

    def _snapshot_lane(self, lane):
        session = self.session(lane["sessionId"])
        job = self.jobs.get(session["activeJob"], {})
        return dict(copy.deepcopy(lane), activeJob=session["activeJob"], availability=session["availability"],
                    worker=self._snapshot_session(session)["worker"], pendingInput=copy.deepcopy(job.get("pendingInput", [])),
                    lastError=session["worker"]["error"])

    def list_lanes(self):
        with self.cv:
            return {"lanes": [self._snapshot_lane(lane) for lane in self.lanes.values()]}

    def show_lane(self, lid):
        with self.cv:
            if lid not in self.lanes:
                raise KeyError("Unknown lane")
            return self._snapshot_lane(self.lanes[lid])

    def lane_action(self, lid, action, data):
        with self.cv:
            self._accepting()
            if lid not in self.lanes:
                raise KeyError("Unknown lane")
            if lid in self.lane_operations:
                raise Conflict("Lane lifecycle operation is in progress")
            lane = self.lanes[lid]
            sid = lane["sessionId"]
            session = self.session(sid)
            if action == "run":
                job = self.turn(sid, text(data, "message"), label=data.get("label"))
                job["laneId"] = lid
                self.jobs[job["id"]]["laneId"] = lid
                self._save_job(self.jobs[job["id"]])
                return job
            if action == "close":
                self.lane_operations.add(lid)
            elif action in {"new", "assign", "delete"}:
                revision = lane["revision"]
                expected = data.get("expectedRevision")
                if expected is not None and type(expected) is not int:
                    raise ValueError("expectedRevision must be an integer")
                if (action == "assign" or expected is not None) and expected != revision:
                    raise Conflict("Lane revision is stale")
                target = self.session(text(data, "sessionId")) if action == "assign" else None
                if target is not None:
                    if target["cwd"] != lane["cwd"] or target["availability"] != "available":
                        raise Conflict("Assigned session must be available in the same working directory")
                    if any(other["id"] != lid and other["sessionId"] == target["id"] for other in self.lanes.values()):
                        raise Conflict("Session is assigned to another lane")
                sids = {sid, target["id"]} if target is not None else {sid}
                self._reserve(*sids)
                if action == "delete":
                    try:
                        if sid in self.workers:
                            raise Conflict("Close the lane worker before removing its registration")
                        del self.lanes[lid]
                        self._save_state()
                        return {"id": lid, "deleted": True}
                    finally:
                        self._release(*sids)
            else:
                raise KeyError("Unknown lane action")
        if action == "close":
            try:
                self.session_action(sid, "close", data)
                return self.show_lane(lid)
            finally:
                with self.cv:
                    self.lane_operations.discard(lid)
                    self.cv.notify_all()
        try:
            if target is None or target["id"] != sid:
                self._stop_worker(sid)
            with self.cv:
                if lane["revision"] != revision or lane["sessionId"] != sid:
                    raise Conflict("Lane assignment changed")
                if target is None:
                    target = self._new_session(lane["name"], lane["cwd"], session["options"])
                    self.sessions[target["id"]] = target
                if target["id"] != sid:
                    lane["history"].append(target["id"])
                    lane["sessionId"] = target["id"]
                    lane["revision"] += 1
                self._save_state()
                return self._snapshot_lane(lane)
        finally:
            self._release(*sids)

    def _owned_session_dir(self, sid):
        identifier(sid)
        parent = self.root / "sessions"
        if parent.is_symlink():
            raise Conflict("Unsafe session storage directory")
        path = parent / sid
        if path.is_symlink():
            raise Conflict("Unsafe session state path")
        return path

    def _delete_session(self, session, data):
        sid = session["id"]
        try:
            if data.get("confirmId") != sid:
                raise ValueError("confirmId must match the session id")
            if session["availability"] != "archived" or sid in self.workers:
                raise Conflict("Only archived, stopped sessions can be deleted")
            if any(lane["sessionId"] == sid for lane in self.lanes.values()):
                raise Conflict("Session is currently assigned to a lane")
            directory = self._owned_session_dir(sid)
            if Path(session["sessionDir"]).absolute() != directory:
                raise Conflict("Unsafe session state path")
            if (self.root / "purge.json").exists():
                raise Conflict("A purge is pending recovery; restart before deleting another session")
            job_ids = [jid for jid, job in self.jobs.items() if job["sessionId"] == sid]
            atomic_json(self.root / "purge.json", {"sessionId": sid, "jobIds": job_ids})
            self._apply_purge(sid, job_ids)
            (self.root / "purge.json").unlink()
            return {"id": sid, "deleted": True}
        finally:
            self._release(sid)

    def _apply_purge(self, sid, job_ids):
        directory = self._owned_session_dir(sid)
        if (self.root / "jobs").is_symlink():
            raise Conflict("Unsafe job storage directory")
        self.sessions.pop(sid, None)
        # Lane history stores only IDs: a deleted ID is a minimal tombstone, not
        # retained name/path/options/content. Current assignments were rejected.
        self._save_state()
        for jid in job_ids:
            identifier(jid)
            job = self.jobs.get(jid)
            if job is not None and job["sessionId"] != sid:
                raise Conflict("Purge intent refers to another session's job")
            self.jobs.pop(jid, None)
            for suffix in (".json", ".jsonl"):
                (self.root / "jobs" / (jid + suffix)).unlink(missing_ok=True)
        if directory.exists():
            shutil.rmtree(directory)

    def _recover_purge(self):
        intent = self.root / "purge.json"
        if not intent.exists():
            return
        data = self._load(intent, {})
        sid = identifier(data["sessionId"])
        if any(lane["sessionId"] == sid for lane in self.lanes.values()):
            raise Conflict("Purge intent targets an assigned session")
        # Accept the first upgrade's path-based intent, but never trust its path.
        if "sessionDir" in data and Path(data["sessionDir"]).absolute() != self._owned_session_dir(sid):
            raise Conflict("Unsafe purge recovery path")
        job_ids = set(data.get("jobIds", []))
        job_ids.update(jid for jid, job in self.jobs.items() if job["sessionId"] == sid)
        self._apply_purge(sid, job_ids)
        intent.unlink()

    def events(self, jid, after):
        if type(after) is not int or after < 0:
            raise ValueError("after must be a non-negative integer")
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
            return {"events": result, "next": result[-1]["seq"] + 1 if result else after}

    def close(self):
        with self.cv:
            self.closing = True
            self._monitor_stop.set()
            self._monitor_wake.set()
            workers = list(self.workers.items())
            for job in self.jobs.values():
                if job["status"] not in TERMINAL:
                    job["cancelRequested"] = True
            for session in self.sessions.values():
                if session["id"] in self.workers:
                    session["worker"]["state"] = "closing"
        if self._monitor_thread is not None and self._monitor_thread is not threading.current_thread():
            self._monitor_thread.join(timeout=2)
        failures = []
        def stop(sid, worker):
            try:
                worker.close()
            except Exception as exc:
                failures.append((sid, str(exc)))
        threads = [threading.Thread(target=stop, args=(sid, worker)) for sid, worker in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with self.cv:
            for sid, worker in workers:
                if not worker.alive and not any(f[0] == sid for f in failures):
                    self.workers.pop(sid, None)
                    self.ready.discard(sid)
                    self.session(sid)["worker"] = self._stopped()
            for job in self.jobs.values():
                if job["status"] not in TERMINAL:
                    self._finish(job, "interrupted", "Daemon shutdown; prompt was not replayed")
            self._save_state()
            self.cv.notify_all()
        if failures:
            raise RuntimeError("Worker shutdown failed: " + "; ".join(error for _, error in failures))
