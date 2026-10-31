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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .lifecycle import Conflict, Store, TERMINAL, private_dir, text
from .webhooks import WebhookDispatcher


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
                result = {"status": "ok", "idleOnlyShutdown": True, "webhooks": 1}
            elif parts == ["webhooks"] and not post:
                result = self.server.webhooks.public_config()
            elif parts == ["webhooks", "reload"] and post:
                self.server.webhooks.reload()
                store.start_monitor()
                result = self.server.webhooks.public_config()
            elif parts == ["webhooks", "test"] and post:
                result = self.server.webhooks.test(data.get("event", "daemon.up"))
            elif parts == ["sessions"]:
                if post:
                    result = store.create(data)
                else:
                    archived = parse_qs(parsed.query).get("archived", ["0"])[0]
                    if archived not in {"0", "1"}:
                        raise ValueError("archived must be 0 or 1")
                    result = store.list_sessions(include_archived=archived == "1")
            elif len(parts) == 2 and parts[0] == "sessions" and not post:
                result = store.show_session(parts[1])
            elif len(parts) == 3 and parts[0] == "sessions" and post:
                if parts[2] == "turn":
                    result = store.turn(parts[1], text(data, "message"), label=data.get("label"))
                elif parts[2] == "compact":
                    result = store.compact(parts[1])
                else:
                    result = store.session_action(parts[1], parts[2], data)
            elif parts == ["lanes"]:
                result = store.create_lane(data) if post else store.list_lanes()
            elif len(parts) == 2 and parts[0] == "lanes" and not post:
                result = store.show_lane(parts[1])
            elif len(parts) == 3 and parts[0] == "lanes" and post:
                result = store.lane_action(parts[1], parts[2], data)
            elif len(parts) == 4 and parts[0] == "jobs" and parts[2] == "agents" and post:
                result = store.agent_action(parts[1], parts[3], data)
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
                elif len(parts) == 3 and post and parts[2] == "deadline":
                    result = store.deadline(jid)
                elif len(parts) == 3 and post:
                    result = store.control(jid, parts[2], data)
                else:
                    raise KeyError("Unknown route")
            elif parts == ["shutdown"] and post:
                store.request_shutdown(data.get("idleOnly", False))
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
            server.webhooks = WebhookDispatcher(root)
            server.store = Store(root, webhooks=server.webhooks)
            server.store.start_monitor()
            def stop(_signum, _frame):
                server.store.request_shutdown()
                threading.Thread(target=server.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            print(json.dumps({"status": "ready", "host": "127.0.0.1", "port": port}), flush=True)
            server.store.notifications.emit("daemon.up", summary="Delegate daemon started")
            server.serve_forever()
        finally:
            try:
                if hasattr(server, "store"):
                    server.store.close()
            finally:
                server.server_close()
                if hasattr(server, "webhooks"):
                    server.webhooks.close()
