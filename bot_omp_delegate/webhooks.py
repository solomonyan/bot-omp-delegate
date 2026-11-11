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

EVENTS = frozenset({"run.done", "run.error", "run.deadline", "run.needs_input", "run.stalled", "lane.idle", "daemon.up", "disk.low"})
_SUBSCRIBER_FIELDS = {"id", "url", "auth_header", "auth_value", "events", "lanes", "hmac_secret", "signature_header", "mode"}
_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_RESERVED = {"content-type", "x-omp-delivery", "x-omp-event", "host", "content-length", "transfer-encoding", "connection", "trailer", "upgrade", "expect", "proxy-authorization"}
CONVERSATIONAL = frozenset({"run.done", "run.error", "run.needs_input"})
_IDENTIFIERS = frozenset({"id", "input_id", "event_id", "job_id", "session_id", "lane_id", "type", "method", "event", "channel"})


def _redact(value, secrets):
    # Structural identifiers/keys are never rewritten; only string VALUES are.
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: item if key in _IDENTIFIERS else _redact(item, secrets) for key, item in value.items()}
    return value


class ConfigError(ValueError):
    """Only static, non-secret diagnostics may be passed here."""


# Tool-approval prompts in these roots are usually cleared by a scoped
# approver within one poll (20 s), so run.needs_input is held for a grace
# period and dropped if answered first. grace 0 restores immediate delivery.
DEFAULT_NEEDS_INPUT_GRACE_SECONDS = 30
DEFAULT_NEEDS_INPUT_GRACE_ROOTS = ("~/lanes/omp-runs/design/launch-2026-11-11/",)
GRACE_SECONDS_ENV = "OMP_DELEGATE_NEEDS_INPUT_GRACE_SECONDS"
GRACE_ROOTS_ENV = "OMP_DELEGATE_NEEDS_INPUT_GRACE_ROOTS"


def _default():
    return {"version": 1, "stall_seconds": 1800, "idle_seconds": None, "subscribers": []}


def _grace_seconds(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 600


def _grace_roots(value):
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() and not any(ord(c) < 32 for c in v) for v in value)


def needs_input_grace(config, environ=None):
    """Effective (seconds, roots). Env overrides config; invalid env is ignored."""
    environ = os.environ if environ is None else environ
    seconds = config.get("needs_input_grace_seconds", DEFAULT_NEEDS_INPUT_GRACE_SECONDS)
    roots = config.get("needs_input_grace_roots", list(DEFAULT_NEEDS_INPUT_GRACE_ROOTS))
    raw = environ.get(GRACE_SECONDS_ENV)
    if raw is not None:
        try:
            value = float(raw)
            if _grace_seconds(value):
                seconds = value
        except ValueError:
            pass
    raw = environ.get(GRACE_ROOTS_ENV)
    if raw is not None:
        roots = [r for r in raw.split(os.pathsep) if r.strip()]
    resolved = tuple(os.path.realpath(os.path.expanduser(r)) for r in roots)
    return seconds, resolved


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
    result["mode"] = item.get("mode", "monitor")
    if not isinstance(result["mode"], str) or result["mode"] not in {"conversation", "monitor"}:
        raise _invalid()
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
    if not isinstance(raw, dict) or set(raw) - {"version", "stall_seconds", "idle_seconds", "subscribers", "needs_input_grace_seconds", "needs_input_grace_roots"} or type(raw.get("version")) is not int or raw["version"] != 1:
        raise _invalid()
    result = {**_default(), **raw}
    if not _duration(result["stall_seconds"]) or not _duration(result["idle_seconds"], True) or not isinstance(result["subscribers"], list):
        raise _invalid()
    if "needs_input_grace_seconds" in raw and not _grace_seconds(raw["needs_input_grace_seconds"]):
        raise _invalid()
    if "needs_input_grace_roots" in raw and not _grace_roots(raw["needs_input_grace_roots"]):
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
    return {**config, "subscribers": [{"id": s["id"], "url": "[redacted]", "auth_configured": "auth_value" in s, "signature_configured": "hmac_secret" in s, "events": list(s.get("events", [])), "lanes": list(s.get("lanes", [])), "mode": s.get("mode", "monitor")} for s in config["subscribers"]]}


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

    def has_subscriber(self, subscriber_id):
        with self._lock:
            return (isinstance(subscriber_id, str) and not self._config_error and not self._closed
                    and any(s["id"] == subscriber_id for s in self._config["subscribers"]))

    @property
    def settings(self):
        with self._lock:
            seconds, roots = needs_input_grace(self._config)
            return {"stall_seconds": self._config["stall_seconds"], "idle_seconds": self._config["idle_seconds"],
                    "needs_input_grace_seconds": seconds, "needs_input_grace_roots": roots}

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

    def _prepare(self, payload, bypass_filters=False, coordinator_subscriber_id=None):
        with self._lock:
            if self._config_error:
                raise _invalid()
            secrets = sorted((s[k] for s in self._config["subscribers"] for k in ("url", "auth_value", "hmac_secret") if s.get(k)), key=len, reverse=True)
            safe = _redact(dict(payload, version=1), secrets)
            # Redact before truncation; otherwise a credential can straddle char 300.
            # Only the bounded summary preview is truncated; full message/input are not.
            safe["summary"] = str(safe.get("summary", ""))[:300]
            event = payload["event"]
            # Event semantics, never payload text/channel overrides, select the route.
            channel = "conversation" if event in CONVERSATIONAL else "monitor"
            safe["channel"] = channel
            if bypass_filters:
                subscribers = list(self._config["subscribers"])
            elif channel == "conversation":
                # Conversational events reach only the recorded owner, overriding
                # that subscriber's filters; ownerless jobs never broadcast.
                subscribers = [s for s in self._config["subscribers"]
                               if coordinator_subscriber_id is not None and s["id"] == coordinator_subscriber_id]
            else:
                # Operational events use ordinary event/lane filters and never
                # reach conversation-mode subscribers.
                subscribers = [
                    s for s in self._config["subscribers"]
                    if s.get("mode", "monitor") == "monitor"
                    and (not s.get("events") or event in s["events"])
                    and (not s.get("lanes") or safe.get("lane") in s["lanes"]
                         or safe.get("lane_id") in s["lanes"])
                ]
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

    def emit(self, payload, coordinator_subscriber_id=None):
        try:
            if self.enabled:
                self._submit(*self._prepare(payload, coordinator_subscriber_id=coordinator_subscriber_id))
        except Exception:
            # Delivery is never allowed to alter a lane's state or expose exceptions.
            self._dropped += 1

    def emit_disk_low(self, *, free_gb, threshold_gb, test=False):
        for value, name in ((free_gb, "free_gb"), (threshold_gb, "threshold_gb")):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ConfigError(f"{name} must be a finite non-negative number")
        if threshold_gb == 0 or (not test and free_gb >= threshold_gb):
            raise ConfigError("disk.low requires free_gb below a positive threshold_gb")
        payload = {
            "event": "disk.low", "lane": "", "lane_id": "", "job_id": "", "status": "test" if test else "low",
            "cwd": "", "branch": "", "last_commit_sha": "",
            "summary": (f"Disk alert test: {free_gb:g} GB free (test)" if test else
                        f"Disk space low: {free_gb:g} GB free (threshold {threshold_gb:g} GB)"),
            "free_gb": free_gb, "threshold_gb": threshold_gb,
            "ts": datetime.now(timezone.utc).isoformat(), "event_id": f"disk.low:{uuid.uuid4()}",
        }
        if test:
            payload["test"] = True
        envelope, subscribers = self._prepare(payload, bypass_filters=True)
        target = next((s for s in subscribers if s["id"] == "jarvis-omp-events"), None)
        if target is None:
            raise ConfigError("Required disk.low webhook subscriber is not configured")
        result = self._deliver(envelope, target)
        return {"configured": 1, "delivered": int(result["delivered"]), "results": [result]}
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
        return {"subscriber_id": subscriber["id"], "delivered": success, "status_code": status,
                "request_id": delivery_id, "attempts": attempts, "error": None if success else error}

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
