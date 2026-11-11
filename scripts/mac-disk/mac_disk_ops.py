#!/usr/bin/env python3
"""Disk threshold alerts, daily measurements and idle-only weekly package cleanup."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HOME = Path.home()
DATA = HOME / "lanes/omp-runs/disk-prevention"
JOBS = HOME / ".omp-delegate/jobs"
FOLDERS = [JOBS, HOME / ".omp/agent/sessions", HOME / ".omp/wt", HOME / ".omp/run",
           HOME / "lanes", Path("/private/tmp"), HOME / "Library/Caches"]
TERMINAL = {"completed", "error", "failed", "aborted", "interrupted"}


def load(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def save(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def log(message):
    with (DATA / "maintenance.log").open("a") as stream:
        stream.write(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {message}\n")


def jobs_idle():
    # Unknown/missing state cannot authorize package-store mutation.
    if not JOBS.is_dir():
        return False
    try:
        for path in JOBS.glob("*.json"):
            job = load(path, None)
            if not isinstance(job, dict) or job.get("status") not in TERMINAL:
                return False
    except (OSError, ValueError):
        return False
    return True


def alert_thresholds(free_gb, state):
    if free_gb > 60:
        state["sent"] = []
        state.pop("notification25", None)
    return [threshold for threshold in (50, 25)
            if free_gb < threshold and threshold not in state.get("sent", [])]


def notify_disk_low(test=False):
    message = "Disk alert test: dispatch is now blocked (test)" if test else "dispatch is now blocked"
    return subprocess.run(["/usr/bin/osascript", "-e",
        f'display notification "{message}" with title "OMP disk low"'],
        capture_output=True, timeout=20)


def watch(free_gb):
    path = DATA / "watch-state.json"
    state = load(path, {"sent": []})
    pending = alert_thresholds(free_gb, state)
    save(path, state)
    if free_gb < 25 and not state.get("notification25"):
        result = notify_disk_low()
        if result.returncode == 0:
            state["notification25"] = True
            save(path, state)
        else:
            log("notification25 failed")
    for threshold in pending:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("disk_emit.py")),
            "--threshold-gb", str(threshold), "--free-gb", str(free_gb)],
            capture_output=True, timeout=90)
        # Never copy transport output (possibly private config) into logs.
        if result.returncode == 0:
            state.setdefault("sent", []).append(threshold)
            save(path, state)
            log(f"disk.low delivered threshold={threshold} free_gb={free_gb:.2f}")
        else:
            log(f"disk.low delivery_failed threshold={threshold}; retry next tick")
    daily_sizes(free_gb)


def daily_sizes(free_gb, *, force=False):
    today = dt.date.today().isoformat()
    path = DATA / "daily-state.json"
    if not force and load(path, {}).get("date") == today:
        return
    sizes = []
    partial = []
    for folder in FOLDERS:
        try:
            result = subprocess.run(["/usr/bin/du", "-sk", str(folder)],
                                    capture_output=True, text=True, timeout=240)
            kb = int(result.stdout.splitlines()[-1].split()[0])
            if kb < 0:
                raise ValueError("negative disk size")
            if result.returncode != 0:
                partial.append(str(folder))
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            kb = None
        sizes.append((str(folder), None if kb is None else round(kb * 1024 / 1e9, 3)))
    sizes.sort(key=lambda row: -1 if row[1] is None else row[1], reverse=True)
    entry = {"date": today, "free_gb": round(free_gb, 3), "folders_gb": dict(sizes),
             "partial_folders": partial}
    with (DATA / "daily-sizes.log").open("a") as stream:
        stream.write(json.dumps(entry) + "\n")
    save(path, {"date": today})
    return entry


def weekly(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    path = DATA / "weekly-state.json"
    state = load(path, {})
    last = state.get("completed")
    if last and (now - dt.datetime.fromisoformat(last)).total_seconds() < 7 * 86400:
        return
    if not jobs_idle():
        log("weekly skipped: running or unknown job state; retry tomorrow")
        return
    for command in (["brew", "cleanup", "-s"], ["pnpm", "store", "prune"]):
        # Re-check before each command; a newly dispatched job blocks the next operation.
        if not jobs_idle():
            log("weekly stopped: job became active; retry tomorrow")
            return
        result = subprocess.run(command, capture_output=True, timeout=1800)
        log(f"weekly {command[0]} exit={result.returncode}")
        if result.returncode:
            return
    save(path, {"completed": now.isoformat()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["watch", "weekly", "daily", "notification-test"])
    parser.add_argument("--refresh", action="store_true", help="append a fresh daily measurement even if already logged")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")
    with (DATA / f"{args.mode}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            if args.mode == "watch":
                watch(shutil.disk_usage(HOME).free / 1e9)
            elif args.mode == "daily":
                entry = daily_sizes(shutil.disk_usage(HOME).free / 1e9, force=args.refresh)
                if entry is not None:
                    print(json.dumps(entry))
            elif args.mode == "notification-test":
                result = notify_disk_low(test=True)
                print(json.dumps({"test": True, "notification_exit_code": result.returncode}))
                if result.returncode:
                    raise SystemExit(result.returncode)
            else:
                weekly()
        except Exception as error:
            log(f"{args.mode} failed type={type(error).__name__}")
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
