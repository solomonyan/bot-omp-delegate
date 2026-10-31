"""Event selection and monotonic inactivity tracking; no network or Git work."""
from __future__ import annotations

from datetime import datetime, timezone
import time


class Notifications:
    def __init__(self, dispatcher=None, clock=None):
        self.dispatcher = dispatcher
        self.clock = clock or time.monotonic
        self.runs = {}
        self.idle = {}
        self.failures = 0

    @property
    def enabled(self):
        return self.dispatcher is not None and self.dispatcher.enabled

    def emit(self, event, job=None, session=None, lane=None, summary=None):
        if not self.enabled:
            return
        job, session, lane = job or {}, session or {}, lane or {}
        payload = {
            "version": 1, "event": event,
            "lane": lane.get("name", ""), "lane_id": lane.get("id", ""),
            "job_id": job.get("id", ""), "status": job.get("status", "idle" if lane else "up"),
            "cwd": session.get("cwd", ""), "branch": "", "last_commit_sha": "",
            "summary": str(summary if summary is not None else job.get("finalMessage") or event),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if job.get("label"):
            payload["label"] = job["label"]
        try:
            # Only bounded enqueue is allowed here: caller may hold Store.cv.
            self.dispatcher.emit(payload)
        except Exception:
            # Notification failure must not change an agent's outcome. No exception
            # text is logged because transports can embed secret endpoint values.
            self.failures += 1

    def started(self, job):
        if self.enabled:
            self._run(job)

    def _run(self, job):
        state = self.runs.get(job["id"])
        if state is None:
            state = {"last_output": self.clock(), "stalled": False, "waiting": False, "input_ids": set()}
            self.runs[job["id"]] = state
        return state

    def output(self, job):
        if self.enabled or job["id"] in self.runs:
            state = self._run(job)
            state.update(last_output=self.clock(), stalled=False)

    def resumed(self, job):
        state = self.runs.get(job["id"])
        if state is not None and state["waiting"] and not job.get("pendingInput"):
            state.update(waiting=False, last_output=self.clock(), stalled=False)

    @staticmethod
    def has_output(frame):
        kind = frame.get("type")
        if kind == "message_update":
            update = frame.get("assistantMessageEvent", {})
            return bool(update.get("delta") or update.get("content"))
        if kind == "message_end":
            return bool(frame.get("message", {}).get("content"))
        if kind == "tool_stream_update":
            return any(bool(frame.get(key)) for key in ("text", "delta", "chunk", "output"))
        if kind in {"tool_execution_update", "tool_execution_end"}:
            result = frame.get("partialResult" if kind == "tool_execution_update" else "result") or {}
            return isinstance(result, dict) and bool(result.get("content"))
        if kind == "subagent_event":
            event = frame.get("payload", {}).get("event")
            return isinstance(event, dict) and Notifications.has_output(event)
        return False

    def needs_input(self, job, session, lane):
        if not self.enabled and job["id"] not in self.runs:
            return
        state = self._run(job)
        state["waiting"] = True
        if not self.enabled:
            return
        for request in job.get("pendingInput", []):
            ui_id = request.get("id")
            if not ui_id or ui_id in state["input_ids"]:
                continue
            state["input_ids"].add(ui_id)
            questions = request.get("questions")
            first = questions[0] if isinstance(questions, list) and questions and isinstance(questions[0], dict) else {}
            summary = request.get("title") or first.get("question") or "Question or approval requires input"
            self.emit("run.needs_input", job, session, lane, summary)

    def finished(self, job, session, lane):
        self.runs.pop(job["id"], None)
        if not self.enabled:
            return
        self.emit("run.done" if job["status"] == "completed" else "run.error", job, session, lane,
                  summary=job.get("error") if job["status"] != "completed" else None)
        if lane:
            self.idle[lane["id"]] = {"marker": (session["id"], session.get("lastActivity")), "since": self.clock(), "sent": False}

    def tick(self, jobs, sessions, lanes, busy):
        if not self.enabled:
            return
        now = self.clock()
        settings = self.dispatcher.settings
        by_session = {lane["sessionId"]: lane for lane in lanes.values()}
        active_ids = set()
        for session in sessions.values():
            jid = session.get("activeJob")
            if not jid or jid not in jobs:
                continue
            active_ids.add(jid)
            job = jobs[jid]
            state = self._run(job)
            lane = by_session.get(session["id"])
            if job["status"] == "waiting_input" or job.get("pendingInput"):
                self.needs_input(job, session, lane)
                continue
            if state["waiting"]:
                state.update(waiting=False, last_output=now, stalled=False)
            if job["status"] == "running" and not state["stalled"] and now - state["last_output"] >= settings["stall_seconds"]:
                state["stalled"] = True
                self.emit("run.stalled", job, session, lane, "No observed output within the configured inactivity interval; work may still be running")
        for jid in set(self.runs) - active_ids:
            self.runs.pop(jid, None)
        for lid in set(self.idle) - set(lanes):
            self.idle.pop(lid, None)
        threshold = settings.get("idle_seconds")
        if threshold is None:
            return
        for lid, lane in lanes.items():
            session = sessions.get(lane["sessionId"])
            if session is None or busy(session["id"]):
                self.idle.pop(lid, None)
                continue
            marker = (session["id"], session.get("lastActivity"))
            state = self.idle.get(lid)
            if state is None or state["marker"] != marker:
                state = self.idle[lid] = {"marker": marker, "since": now, "sent": False}
            if not state["sent"] and now - state["since"] >= threshold:
                state["sent"] = True
                self.emit("lane.idle", session=session, lane=lane, summary="Lane idle beyond the configured interval")
