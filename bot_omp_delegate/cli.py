from __future__ import annotations

import argparse
import json
import math
import os
import fcntl
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_PORT = 4510
TERMINAL = {"completed", "aborted", "error", "interrupted"}


def _state_dir(value: str | None) -> Path:
    return Path(value or os.environ.get("OMP_DELEGATE_STATE", "~/.omp-delegate")).expanduser().resolve()


def _port(value: int | None) -> int:
    return value if value is not None else int(os.environ.get("OMP_DELEGATE_PORT", DEFAULT_PORT))


class Client:
    def __init__(self, state: Path, port: int):
        self.state = state
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        # Bypass configured proxies for local daemon credentials and prompts.
        self.transport = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            self.token = (state / "token").read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read daemon token at {state / 'token'}: {exc}") from exc
        if not self.token:
            raise RuntimeError(f"daemon token at {state / 'token'} is empty")

    def request(self, method: str, route: str, body: dict | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + route, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"})
        try:
            timeout = 150 if method == "POST" else 75
            with self.transport.open(req, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                try:
                    detail = json.loads(exc.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    detail = exc.reason
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"daemon request failed: {exc}") from exc
        try:
            return json.loads(payload.decode("utf-8")) if payload else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("daemon returned invalid JSON") from exc


def _emit(value: Any, *, flush: bool = True) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=flush)


def _wait(client: Client, job_id: str, max_hours: float | None, interrupt_at_deadline: bool) -> int:
    deadline = None if max_hours is None else time.monotonic() + max_hours * 3600
    last_pending: str | None = None
    route = f"/jobs/{urllib.parse.quote(job_id)}"
    snapshot = client.request("GET", route)
    while True:
        if snapshot.get("status") == "waiting_input":
            pending = json.dumps(snapshot.get("pendingInput", []), ensure_ascii=False)
            if pending != last_pending:
                print(f"Waiting for input (use omp-delegate answer {job_id} --json '{{...}}'): {pending}", file=sys.stderr, flush=True)
                last_pending = pending
        else:
            last_pending = None
        if snapshot.get("status") in TERMINAL:
            _emit(snapshot)
            if snapshot["status"] == "completed":
                return 0
            if snapshot["status"] in {"aborted", "interrupted"}:
                return 2
            return 1
        remaining = 60 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            if interrupt_at_deadline:
                _emit(client.request("POST", route + "/interrupt", {}))
            return 3
        snapshot = client.request("GET", route + f"/wait?timeout={min(60, remaining)}")


def _worker_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model")
    parser.add_argument("--thinking")
    parser.add_argument("--profile")
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--tools")
    parser.add_argument("--approval-mode", choices=("yolo", "write", "always-ask", "always-ask-cached"))


def _options(args: argparse.Namespace) -> dict[str, Any]:
    values = vars(args)
    options = {key: values[key] for key in ("model", "thinking", "profile", "tools", "approval_mode")
               if values.get(key) is not None}
    for field in ("config", "add_dir"):
        paths = values.get(field) or []
        if paths:
            normalized = []
            for value in paths:
                path = Path(value).expanduser()
                if not path.is_absolute():
                    raise ValueError(f"--{field.replace('_', '-')} paths must be absolute: {value}")
                normalized.append(str(path.resolve()))
            options[field] = normalized
    return options


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omp-delegate", description="Manage OMP delegated sessions and jobs.")
    parser.add_argument("--state-dir", help="daemon state directory (default: ~/.omp-delegate)")
    parser.add_argument("--port", type=int, help="loopback daemon port (default: 4510)")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("serve", help="run the daemon in the foreground")
    daemon = subs.add_parser("daemon", help="manage the detached daemon")
    daemon.add_argument("action", choices=("start", "status", "stop", "restart"))
    session = subs.add_parser("session", help="manage sessions")
    sess = session.add_subparsers(dest="action", required=True)
    create = sess.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--cwd", required=True)
    _worker_options(create)
    listing = sess.add_parser("list")
    listing.add_argument("--archived", action="store_true")
    for action in ("show", "open", "compact", "archive", "unarchive"):
        command = sess.add_parser(action)
        command.add_argument("id")
    close = sess.add_parser("close")
    close.add_argument("id")
    close.add_argument("--interrupt", action="store_true")
    configure = sess.add_parser("configure")
    configure.add_argument("id")
    _worker_options(configure)
    delete = sess.add_parser("delete")
    delete.add_argument("id")
    delete.add_argument("--confirm-id", required=True)
    lane = subs.add_parser("lane", help="manage work lanes")
    lanes = lane.add_subparsers(dest="action", required=True)
    lane_create = lanes.add_parser("create")
    lane_create.add_argument("--name", required=True)
    lane_create.add_argument("--cwd", required=True)
    _worker_options(lane_create)
    lanes.add_parser("list")
    for action in ("show", "new"):
        command = lanes.add_parser(action)
        command.add_argument("id")
    run = lanes.add_parser("run")
    run.add_argument("id")
    run.add_argument("message")
    run.add_argument("--detach", action="store_true")
    run.add_argument("--max-hours", type=float, default=6)
    run.add_argument("--interrupt-at-deadline", action="store_true")
    close_lane = lanes.add_parser("close")
    close_lane.add_argument("id")
    close_lane.add_argument("--interrupt", action="store_true")
    assign = lanes.add_parser("assign")
    assign.add_argument("id")
    assign.add_argument("--session", required=True)
    assign.add_argument("--expected-revision", type=int, required=True)
    delete_lane = lanes.add_parser("delete")
    delete_lane.add_argument("id")
    delete_lane.add_argument("--expected-revision", type=int)
    agent = subs.add_parser("agent", help="inspect or control a job subagent")
    agents = agent.add_subparsers(dest="action", required=True)
    agent_list = agents.add_parser("list")
    agent_list.add_argument("job")
    agent_read = agents.add_parser("read")
    agent_read.add_argument("job")
    agent_read.add_argument("agent")
    agent_read.add_argument("--from-byte", type=int, default=0)
    for action in ("steer", "cancel"):
        command = agents.add_parser(action)
        command.add_argument("job")
        command.add_argument("agent")
        if action == "steer":
            command.add_argument("message")
    turn = subs.add_parser("turn", help="send a prompt to a session")
    turn.add_argument("session")
    turn.add_argument("message")
    turn.add_argument("--detach", action="store_true")
    turn.add_argument("--max-hours", type=float, default=6)
    turn.add_argument("--interrupt-at-deadline", action="store_true")
    wait = subs.add_parser("wait", help="wait for a job to finish")
    wait.add_argument("job")
    wait.add_argument("--max-hours", type=float, default=6)
    wait.add_argument("--interrupt-at-deadline", action="store_true")
    read = subs.add_parser("read", help="read the current job snapshot")
    read.add_argument("job")
    events = subs.add_parser("events", help="read job events")
    events.add_argument("job")
    events.add_argument("--after", type=int, default=0)
    steer = subs.add_parser("steer", help="steer a running job")
    steer.add_argument("job")
    steer.add_argument("message")
    interrupt = subs.add_parser("interrupt", help="interrupt a running job")
    interrupt.add_argument("job")
    answer = subs.add_parser("answer", help="answer a pending RPC UI request")
    answer.add_argument("job")
    answer.add_argument("--json", required=True, dest="answer_json")
    return parser


def _daemon(args: argparse.Namespace, state: Path, port: int) -> int:
    if args.action == "start":
        try:
            Client(state, port).request("GET", "/health")
            _emit({"status": "running", "port": port})
            return 0
        except RuntimeError:
            pass
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        log_path = state / "daemon.log"
        command = [sys.executable, "-m", "bot_omp_delegate.cli", "--state-dir", str(state), "--port", str(port), "serve"]
        package_root = str(Path(__file__).resolve().parent.parent)
        env = os.environ.copy()
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        with log_path.open("ab") as log:
            os.chmod(log_path, 0o600)
            process = subprocess.Popen(command, cwd=package_root, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True, env=env,
                                       close_fds=True)
        for _ in range(50):
            try:
                Client(state, port).request("GET", "/health")
                _emit({"status": "running", "port": port})
                return 0
            except RuntimeError:
                if process.poll() is not None:
                    raise RuntimeError(f"daemon exited during startup; see {log_path}")
                time.sleep(0.1)
        raise RuntimeError(f"daemon did not become ready on port {port}; see {log_path}")
    if args.action == "status":
        try:
            result = Client(state, port).request("GET", "/health")
            _emit({"status": "running", "port": port, "health": result})
            return 0
        except RuntimeError:
            _emit({"status": "stopped", "port": port})
            return 1
    if args.action == "stop":
        result = Client(state, port).request("POST", "/shutdown", {})
        _await_shutdown(state)
        _emit(result)
        return 0
    try:
        Client(state, port).request("POST", "/shutdown", {})
    except RuntimeError:
        pass
    _await_shutdown(state)
    args.action = "start"
    return _daemon(args, state, port)


def _await_shutdown(state: Path) -> None:
    path = state / "daemon.lock"
    if not path.exists():
        return
    deadline = time.monotonic() + 45
    with path.open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Daemon did not finish shutdown within 45 seconds")
                time.sleep(0.1)


def _encoded(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    state = _state_dir(args.state_dir)
    port = _port(args.port)
    try:
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if hasattr(args, "max_hours") and (not math.isfinite(args.max_hours) or args.max_hours <= 0):
            raise ValueError("max-hours must be finite and positive")
        if hasattr(args, "after") and args.after < 0:
            raise ValueError("after must be nonnegative")
        if hasattr(args, "from_byte") and args.from_byte < 0:
            raise ValueError("from-byte must be nonnegative")
        if getattr(args, "expected_revision", None) is not None and args.expected_revision < 0:
            raise ValueError("expected-revision must be nonnegative")
        if args.command == "serve":
            from .server import serve
            serve(state, port)
            return 0
        if args.command == "daemon":
            return _daemon(args, state, port)
        client = Client(state, port)
        if args.command == "session":
            base = f"/sessions/{_encoded(args.id)}" if hasattr(args, "id") else "/sessions"
            if args.action == "list":
                _emit(client.request("GET", "/sessions?archived=1" if args.archived else "/sessions"))
            elif args.action == "create":
                body = {"name": args.name, "cwd": str(Path(args.cwd).expanduser().resolve()), "options": _options(args)}
                _emit(client.request("POST", "/sessions", body))
            elif args.action == "show":
                _emit(client.request("GET", base))
            elif args.action == "compact":
                _emit(client.request("POST", base + "/compact", {}))
            elif args.action == "configure":
                _emit(client.request("POST", base + "/configure", {"options": _options(args)}))
            elif args.action == "delete":
                _emit(client.request("POST", base + "/delete", {"confirmId": args.confirm_id}))
            elif args.action in {"open", "archive", "unarchive"}:
                _emit(client.request("POST", base + "/" + args.action, {}))
            elif args.action == "close":
                _emit(client.request("POST", base + "/close", {"interrupt": args.interrupt}))
            else:
                _emit(client.request("POST", base + "/delete", {"confirmId": args.confirm_id}))
            return 0
        if args.command == "lane":
            if args.action == "create":
                body = {"name": args.name, "cwd": str(Path(args.cwd).expanduser().resolve()), "options": _options(args)}
                _emit(client.request("POST", "/lanes", body))
            elif args.action == "list":
                _emit(client.request("GET", "/lanes"))
            elif args.action == "show":
                _emit(client.request("GET", f"/lanes/{_encoded(args.id)}"))
            elif args.action in {"run", "close", "new"}:
                body = {"message": args.message} if args.action == "run" else (
                    {"interrupt": args.interrupt} if args.action == "close" else {})
                result = client.request("POST", f"/lanes/{_encoded(args.id)}/{args.action}", body)
                _emit(result)
                if args.action == "run" and not args.detach:
                    return _wait(client, result["id"], args.max_hours, args.interrupt_at_deadline)
            elif args.action == "assign":
                body = {"sessionId": args.session, "expectedRevision": args.expected_revision}
                _emit(client.request("POST", f"/lanes/{_encoded(args.id)}/assign", body))
            else:
                body = {} if args.expected_revision is None else {"expectedRevision": args.expected_revision}
                _emit(client.request("POST", f"/lanes/{_encoded(args.id)}/delete", body))
            return 0
        if args.command == "agent":
            route = f"/jobs/{_encoded(args.job)}/agents/{args.action}"
            body: dict[str, Any] = {}
            if args.action != "list":
                body["subagentId"] = args.agent
            if args.action == "read":
                body["fromByte"] = args.from_byte
            elif args.action == "steer":
                body["message"] = args.message
            _emit(client.request("POST", route, body))
            return 0
        if args.command == "turn":
            job = client.request("POST", f"/sessions/{_encoded(args.session)}/turn", {"message": args.message})
            _emit(job)
            return 0 if args.detach else _wait(client, job["id"], args.max_hours, args.interrupt_at_deadline)
        if args.command == "wait":
            return _wait(client, args.job, args.max_hours, args.interrupt_at_deadline)
        if args.command == "read":
            _emit(client.request("GET", f"/jobs/{_encoded(args.job)}"))
        elif args.command == "events":
            _emit(client.request("GET", f"/jobs/{_encoded(args.job)}/events?after={args.after}"))
        elif args.command == "steer":
            _emit(client.request("POST", f"/jobs/{_encoded(args.job)}/steer", {"message": args.message}))
        elif args.command == "interrupt":
            _emit(client.request("POST", f"/jobs/{_encoded(args.job)}/interrupt", {}))
        elif args.command == "answer":
            try:
                answer = json.loads(args.answer_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"--json must be valid JSON: {exc}") from exc
            if not isinstance(answer, dict):
                raise RuntimeError("--json must decode to a JSON object")
            _emit(client.request("POST", f"/jobs/{_encoded(args.job)}/answer", answer))
        return 0
    except (RuntimeError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
