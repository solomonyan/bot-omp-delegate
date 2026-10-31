"""Private subscriber configuration and bounded, best-effort outbound delivery."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

EVENTS = frozenset({"run.done", "run.error", "run.deadline", "run.needs_input", "run.stalled", "lane.idle", "daemon.up"})
_SUBSCRIBER_FIELDS = {"id", "url", "auth_header", "auth_value", "events", "lanes", "hmac_secret", "signature_header"}
_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_RESERVED = {"content-type", "x-omp-delivery", "x-omp-event", "host", "content-length", "transfer-encoding", "connection", "trailer", "upgrade", "expect", "proxy-authorization"}


class ConfigError(ValueError):
    """Only static, non-secret diagnostics may be passed here."""


def _default():
    return {"version": 1, "stall_seconds": 1800, "idle_seconds": None, "subscribers": []}


def _invalid():
    return ConfigError("Invalid webhook configuration")


def _duration(value, nullable=False):
    try:
        return (nullable and value is None) or (type(value) in (int, float) and math.isfinite(value) and value > 0)
    except OverflowError:
        return False


def _subscriber(item):
    if not isinstance(item, dict) or set(item) - _SUBSCRIBER_FIELDS:
        raise _invalid()
    if not isinstance(item.get("id"), str) or not _ID.fullmatch(item["id"]):
        raise _invalid()
    url = item.get("url")
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url or "#" in url:
        raise _invalid()
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
        loopback = host == "localhost"
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None or (port is not None and not 1 <= port <= 65535) or (parsed.scheme == "http" and not loopback):
            raise _invalid()
        url.encode("utf-8")
    except (ValueError, UnicodeError):
        raise _invalid() from None
    result = dict(item)
    result["signature_header"] = item.get("signature_header", "X-OMP-Signature")
    for key in ("auth_header", "signature_header"):
        if key in result and (not isinstance(result[key], str) or not _HEADER.fullmatch(result[key]) or result[key].lower() in _RESERVED):
            raise _invalid()
    if "auth_header" in item or "auth_value" in item:
        value = item.get("auth_value")
        if not isinstance(item.get("auth_header"), str) or not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 or ord(c) > 255 for c in value):
            raise _invalid()
        if item["auth_header"].lower() == result["signature_header"].lower():
            raise _invalid()
    if "hmac_secret" in item:
        try:
            if not isinstance(item["hmac_secret"], str) or not item["hmac_secret"]:
                raise _invalid()
            item["hmac_secret"].encode("utf-8")
        except UnicodeError:
            raise _invalid() from None
    for key in ("events", "lanes"):
        if key in item and (not isinstance(item[key], list) or any(not isinstance(v, str) or not v or any(ord(c) < 32 for c in v) for v in item[key])):
            raise _invalid()
    if any(v not in EVENTS for v in item.get("events", [])):
        raise _invalid()
    return result


def _config(raw):
    if not isinstance(raw, dict) or set(raw) - {"version", "stall_seconds", "idle_seconds", "subscribers"} or type(raw.get("version")) is not int or raw["version"] != 1:
        raise _invalid()
    result = {**_default(), **raw}
    if not _duration(result["stall_seconds"]) or not _duration(result["idle_seconds"], True) or not isinstance(result["subscribers"], list):
        raise _invalid()
    result["subscribers"] = [_subscriber(s) for s in result["subscribers"]]
    if len({s["id"] for s in result["subscribers"]}) != len(result["subscribers"]):
        raise _invalid()
    return result


@contextmanager
def _locked(root):
    fd = None
    try:
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise _invalid()
        fd = os.open(root / "webhooks.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _invalid()
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    except OSError:
        raise _invalid() from None
    finally:
        if fd is not None:
            os.close(fd)


def _read(root):
    path = root / "webhooks.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _default()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise _invalid()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise _invalid()
            raw = json.load(stream)
    except (OSError, ValueError, UnicodeError):
        raise _invalid() from None
    return _config(raw)


def load_config(root):
    with _locked(root) as directory:
        return _read(directory)


def _safe(config):
    return {**config, "subscribers": [{"id": s["id"], "url": "[redacted]", "auth_configured": "auth_value" in s, "signature_configured": "hmac_secret" in s, "events": list(s.get("events", [])), "lanes": list(s.get("lanes", []))} for s in config["subscribers"]]}


def list_config(root):
    return _safe(load_config(root))


def _write(root, config):
    temp = root / (".webhooks." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(config, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, root / "webhooks.json")
    except (OSError, UnicodeError):
        raise _invalid() from None
    finally:
        temp.unlink(missing_ok=True)


def add_subscriber(root, subscriber):
    item = _subscriber(subscriber)
    with _locked(root) as directory:
        config = _read(directory)
        if any(s["id"] == item["id"] for s in config["subscribers"]):
            raise ConfigError("Webhook subscriber already exists")
        config["subscribers"].append(item)
        _write(directory, config)
        return _safe(config)


def remove_subscriber(root, id):
    with _locked(root) as directory:
        config = _read(directory)
        remaining = [s for s in config["subscribers"] if s["id"] != id]
        if len(remaining) == len(config["subscribers"]):
            raise ConfigError("Webhook subscriber not found")
        config["subscribers"] = remaining
        _write(directory, config)
        return _safe(config)


def _git(cwd):
    values = []
    for args in (("--abbrev-ref", "HEAD"), ("HEAD",)):
        try:
            proc = subprocess.run(["git", "-C", cwd, "rev-parse", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1, check=False, text=True)
            values.append(proc.stdout.strip() if proc.returncode == 0 else "")
        except (OSError, subprocess.SubprocessError, UnicodeError):
            values.append("")
    return values


class _Envelope:
    def __init__(self, payload):
        self.payload = payload
        self._body = None
        self._lock = threading.Lock()

    def body(self):
        with self._lock:
            if self._body is None:
                enriched = dict(self.payload)
                enriched["branch"], enriched["last_commit_sha"] = _git(enriched["cwd"]) if enriched.get("cwd") else ("", "")
                self._body = json.dumps(enriched, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            return self._body


class _Batch:
    def __init__(self, subscribers):
        self.results = [{"subscriber_id": s["id"], "delivered": False, "status_code": None, "attempts": 0, "error": "delivery_timeout"} for s in subscribers]
        self.remaining = len(subscribers)
        self.lock = threading.Lock()
        self.done = threading.Event()

    def complete(self, index, result):
        with self.lock:
            self.results[index] = result
            self.remaining -= 1
            if not self.remaining:
                self.done.set()


def _request(subscriber, body, event, delivery_id, timeout):
    parsed = urlsplit(subscriber["url"])
    connection = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = connection(parsed.hostname, parsed.port, timeout=timeout)
    headers = {"Content-Type": "application/json", "X-OMP-Delivery": delivery_id, "X-OMP-Event": event}
    if "auth_header" in subscriber:
        headers[subscriber["auth_header"]] = subscriber["auth_value"]
    if "hmac_secret" in subscriber:
        headers[subscriber["signature_header"]] = "sha256=" + hmac.new(subscriber["hmac_secret"].encode(), body, hashlib.sha256).hexdigest()
    expired = threading.Event()
    def expire():
        expired.set()
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    # Socket inactivity timeouts alone do not bound trickled status/headers.
    watchdog = threading.Timer(timeout, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        conn.connect()
        # System DNS resolution cannot be interrupted by a socket shutdown.
        # If it returns after expiry, do not send a late authenticated request.
        if expired.is_set():
            return False, None, "network_error"
        conn.request("POST", urlunsplit(("", "", parsed.path or "/", parsed.query, "")), body=body, headers=headers)
        status = conn.getresponse().status
        if expired.is_set():
            return False, None, "network_error"
        # Never follow redirects, use proxies, or read/echo credential-bearing bodies.
        return 200 <= status < 300, status, None if 200 <= status < 300 else "http_error"
    except (OSError, http.client.HTTPException, UnicodeError, ValueError):
        return False, None, "network_error"
    finally:
        watchdog.cancel()
        conn.close()


class WebhookDispatcher:
    def __init__(self, root, *, retry_delays=(1, 2, 4), request_timeout=5, workers=4, queue_size=256):
        self.root = Path(root)
        self.retry_delays = tuple(retry_delays)
        self.request_timeout = request_timeout
        self.workers = max(1, min(4, workers))
        self._queue = queue.Queue(maxsize=max(1, queue_size))
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._threads = []
        self._dropped = 0
        self._config_error = None
        self._closed = False
        try:
            self._config = load_config(root)
        except ConfigError:
            self._config = _default()
            self._config_error = "invalid_configuration"

    @property
    def enabled(self):
        with self._lock:
            return bool(self._config["subscribers"]) and not self._config_error and not self._closed

    @property
    def settings(self):
        with self._lock:
            return {"stall_seconds": self._config["stall_seconds"], "idle_seconds": self._config["idle_seconds"]}

    def public_config(self):
        with self._lock:
            result = _safe(self._config)
            if self._config_error:
                result["config_error"] = self._config_error
            return result

    def reload(self):
        config = load_config(self.root)
        with self._lock:
            self._config, self._config_error = config, None
        return self.public_config()

    def _prepare(self, payload, bypass_filters=False):
        with self._lock:
            if self._config_error:
                raise _invalid()
            subscribers = [s for s in self._config["subscribers"] if bypass_filters or ((not s.get("events") or payload["event"] in s["events"]) and (not s.get("lanes") or payload.get("lane") in s["lanes"] or payload.get("lane_id") in s["lanes"]))]
            safe = dict(payload, version=1)
            summary = str(safe.get("summary", ""))
            # Redact before truncation; otherwise a credential can straddle char 300.
            secrets = {s[k] for s in self._config["subscribers"] for k in ("url", "auth_value", "hmac_secret") if s.get(k)}
            for secret in sorted(secrets, key=len, reverse=True):
                summary = summary.replace(secret, "[redacted]")
            safe["summary"] = summary[:300]
            return _Envelope(safe), subscribers

    def _submit(self, envelope, subscribers, batch=None):
        with self._lock:
            if subscribers and not self._threads and not self._closed:
                self._threads = [threading.Thread(target=self._worker, daemon=True, name="webhook-delivery") for _ in range(self.workers)]
                for thread in self._threads:
                    thread.start()
            for index, subscriber in enumerate(subscribers):
                try:
                    if self._closed:
                        raise queue.Full()
                    self._queue.put_nowait((envelope, subscriber, batch, index))
                except queue.Full:
                    self._dropped += 1
                    if batch:
                        batch.complete(index, {"subscriber_id": subscriber["id"], "delivered": False, "status_code": None, "attempts": 0, "error": "queue_full"})

    def emit(self, payload):
        try:
            if self.enabled:
                self._submit(*self._prepare(payload))
        except Exception:
            # Delivery is never allowed to alter a lane's state or expose exceptions.
            self._dropped += 1

    def test(self, event="daemon.up"):
        if not isinstance(event, str) or event not in EVENTS:
            raise ConfigError("Unknown webhook event")
        payload = {"event": event, "lane": "", "lane_id": "", "job_id": "", "status": "up" if event == "daemon.up" else "test", "cwd": "", "branch": "", "last_commit_sha": "", "summary": "Webhook connectivity test", "ts": datetime.now(timezone.utc).isoformat()}
        envelope, subscribers = self._prepare(payload, bypass_filters=True)
        if not subscribers:
            return {"configured": 0, "delivered": 0, "results": []}
        batch = _Batch(subscribers)
        self._submit(envelope, subscribers, batch)
        batch.done.wait(min(120, len(subscribers) * (self.request_timeout * (len(self.retry_delays) + 1) + sum(self.retry_delays)) + 3))
        with batch.lock:
            results = [dict(result) for result in batch.results]
        return {"configured": len(subscribers), "delivered": sum(r["delivered"] for r in results), "results": results}

    def _deliver(self, envelope, subscriber):
        body = envelope.body()
        delivery_id = str(uuid.uuid4())
        attempts, success, status, error = 0, False, None, "stopped"
        for attempt in range(len(self.retry_delays) + 1):
            if self._stop.is_set():
                break
            attempts += 1
            success, status, error = _request(subscriber, body, envelope.payload["event"], delivery_id, self.request_timeout)
            if success:
                break
            if attempt < len(self.retry_delays) and self._stop.wait(self.retry_delays[attempt]):
                error = "stopped"
                break
        return {"subscriber_id": subscriber["id"], "delivered": success, "status_code": status, "attempts": attempts, "error": None if success else error}

    def _worker(self):
        while not self._stop.is_set():
            try:
                envelope, subscriber, batch, index = self._queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                result = self._deliver(envelope, subscriber)
            except Exception:
                result = {"subscriber_id": subscriber["id"], "delivered": False, "status_code": None, "attempts": 0, "error": "delivery_error"}
            finally:
                self._queue.task_done()
            if batch:
                batch.complete(index, result)

    def close(self, drain_timeout=1):
        with self._lock:
            self._closed = True
        deadline = time.monotonic() + drain_timeout
        # Queue.join() has no timeout; use its completion condition for a bounded
        # grace period so orderly shutdown errors get a delivery opportunity.
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._queue.all_tasks_done.wait(remaining)
        self._stop.set()
        deadline = time.monotonic() + 1
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
