#!/usr/bin/env python3
"""Conservative cleanup of explicitly bounded macOS verification leftovers."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import fnmatch
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import shlex

HOME = Path.home()
STATE_DIR = HOME / "lanes" / "omp-runs" / "disk-prevention"
LOG_PATH = STATE_DIR / "reaper.log"
LOCK_PATH = STATE_DIR / "reaper.lock"
JOBS_DIR = HOME / ".omp-delegate" / "jobs"
LANE_CLI = HOME / "Workspace" / "bot-omp-delegate" / "scripts" / "omp-delegate"
PROTECTED_NAMES = {
    "wellness-ops-hk-payments", "wellness-ops-settings-ux",
    "wellness-ops-main-baseline", "wellness-ops-pglite-tests",
    "wellness-ops-a", "wellness-ops-b", "wellness-ops-c",
    "bot-omp-delegate-disk", "A", "B", "C",
}
PROTECTED_PREFIXES = ("verify-521-", "verify-522-", "wellness-ops")
PROTECTED_RELATIVE_PATHS = (
    "Workspace", ".omp/agent/sessions", "orca", "Docker", ".docker",
    "Library/Containers/com.docker.docker",
)
PROTECTED_COMPONENTS = {"orca", "workspaces", "design", "design-packs", "design_packs", "designpacks"}
PROTECTED_NAME_FRAGMENTS = ("docker",)
PROTECTED_FILE_GLOBS = ("ANSWER*.md",)
OWNER_FILE = ".omp-isolation-owner.json"
DAY = 24 * 60 * 60
TERMINAL_STATES = {
    "done", "completed", "failed", "error", "cancelled", "canceled", "stopped",
    "timed_out", "expired", "interrupted", "aborted",
}


class SafetyError(Exception):
    """A safety condition could not be established."""


def _run(args: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _path(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _protected_path(candidate: Path) -> bool:
    home = HOME.resolve()
    resolved = candidate.resolve(strict=False)
    fixed = (home / name for name in PROTECTED_RELATIVE_PATHS)
    if any(_overlaps(resolved, path) for path in fixed):
        return True
    return any(
        part in PROTECTED_NAMES
        or part.startswith(PROTECTED_PREFIXES)
        or part.lower() in PROTECTED_COMPONENTS
        or any(fragment in part.lower() for fragment in PROTECTED_NAME_FRAGMENTS)
        for part in resolved.parts
    )




def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_start(pid: int) -> str | None:
    # `ps` is read-only. On macOS lstart identifies PID reuse conservatively.
    try:
        result = _run(["/bin/ps", "-o", "lstart=", "-p", str(pid)])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    return " ".join(result.stdout.split())


def _timestamp(value: object) -> dt.datetime | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = dt.datetime.strptime(text, "%a %b %d %H:%M:%S %Y")
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _same_start_time(recorded: object, current: object) -> bool | None:
    first, second = _timestamp(recorded), _timestamp(current)
    if first is None or second is None:
        return None
    if isinstance(first, dt.datetime) and isinstance(second, dt.datetime):
        return abs(first.timestamp() - second.timestamp()) <= 1.5
    if isinstance(first, float) and isinstance(second, float):
        return abs(first - second) <= 1.5
    if isinstance(first, float) and isinstance(second, dt.datetime):
        return abs(second.timestamp() - first) <= 1.5
    if isinstance(second, float) and isinstance(first, dt.datetime):
        return abs(first.timestamp() - second) <= 1.5
    return None


def _owner_is_orphan(candidate: Path) -> bool:
    try:
        owner = json.loads((candidate / OWNER_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(owner, dict) or type(owner.get("pid")) is not int or owner["pid"] <= 0:
        return False
    pid = owner["pid"]
    if not _pid_is_alive(pid):
        return True
    recorded = owner.get("start_time", owner.get("started_at", owner.get("process_start", owner.get("startTime"))))
    return _same_start_time(recorded, _process_start(pid)) is False


def discover_candidates(home: Path | None = None, temp: Path = Path("/private/tmp")) -> list[Path]:
    home = (home or HOME).expanduser().resolve()
    candidates: set[Path] = set()
    try:
        for candidate in (home / "lanes").glob("verify-*"):
            if candidate.is_dir() and not candidate.is_symlink():
                candidates.add(candidate.resolve())
    except OSError:
        pass
    try:
        for candidate in (home / ".omp" / "wt").glob("*"):
            if candidate.is_dir() and not candidate.is_symlink() and _owner_is_orphan(candidate):
                candidates.add(candidate.resolve())
    except OSError:
        pass
    now = time.time()
    try:
        for candidate in temp.iterdir():
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            name = candidate.name
            eligible = name.startswith("verify") or "-pg" in name or (name.startswith("pr") and "-review" in name)
            try:
                old = now - candidate.stat().st_mtime >= DAY
            except OSError:
                continue
            if eligible and old:
                candidates.add(candidate.resolve())
    except OSError:
        pass
    return sorted(candidates, key=str)


def _job_paths(value: object) -> list[Path]:
    if not isinstance(value, dict):
        raise SafetyError("job metadata is not an object")
    paths: list[Path] = []
    for key in ("cwd", "working_directory", "workdir"):
        path = _path(value.get(key))
        if path:
            paths.append(path)
    for key in ("add_dirs", "add_dirs_paths", "additional_directories"):
        entries = value.get(key, [])
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise SafetyError("job directory metadata is unknown")
        for entry in entries:
            value = entry if isinstance(entry, str) else entry.get("path") if isinstance(entry, dict) else None
            path = _path(value)
            if path:
                paths.append(path)
    return paths


def _job_status(value: object) -> str:
    if not isinstance(value, dict):
        raise SafetyError("job metadata is not an object")
    state = value.get("status", value.get("state"))
    if not isinstance(state, str):
        raise SafetyError("job state is unknown")
    return state.lower().replace("-", "_")


def _active_job_paths(jobs_dir: Path = JOBS_DIR) -> tuple[list[Path], set[str]]:
    if not jobs_dir.is_dir():
        raise SafetyError("job metadata directory unavailable")
    paths: list[Path] = []
    session_ids: set[str] = set()
    try:
        entries = list(jobs_dir.glob("*.json"))
    except OSError as exc:
        raise SafetyError("job metadata cannot be inspected") from exc
    for file in entries:
        try:
            value = json.loads(file.read_text(encoding="utf-8"))
            state = _job_status(value)
            if state in {"running", "waiting_input"}:
                paths.extend(_job_paths(value))
                session_id = value.get("sessionId")
                if isinstance(session_id, str) and session_id:
                    session_ids.add(session_id)
                elif not any(_path(value.get(key)) for key in ("cwd", "working_directory", "workdir")):
                    raise SafetyError("active job location is unknown")
            elif state not in TERMINAL_STATES:
                raise SafetyError("job state is unknown")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SafetyError("job metadata cannot be inspected") from exc
    return paths, session_ids


def _session_paths(session_ids: set[str], state_file: Path = HOME / ".omp-delegate" / "state.json") -> list[Path]:
    if not session_ids:
        return []
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SafetyError("active session location cannot be inspected") from exc
    sessions = state.get("sessions") if isinstance(state, dict) else None
    if not isinstance(sessions, dict):
        raise SafetyError("active session state is unknown")
    paths: list[Path] = []
    for session_id in session_ids:
        session = sessions.get(session_id)
        if not isinstance(session, dict):
            raise SafetyError("active session location is unknown")
        cwd = _path(session.get("cwd"))
        options = session.get("options")
        if cwd is None or not isinstance(options, dict):
            raise SafetyError("active session location is unknown")
        paths.append(cwd)
        add_dirs = options.get("add_dir", [])
        if not isinstance(add_dirs, list):
            raise SafetyError("active session add-dir state is unknown")
        for entry in add_dirs:
            path = _path(entry)
            if path is None:
                raise SafetyError("active session add-dir state is unknown")
            paths.append(path)
    return paths


def _lane_paths(cli: Path = LANE_CLI) -> list[Path]:
    if not cli.is_file():
        raise SafetyError("lane query unavailable")
    try:
        result = _run([str(cli), "lane", "list"], timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SafetyError("lane query unavailable") from exc
    if result.returncode:
        raise SafetyError("lane query failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SafetyError("lane query state is unknown") from exc
    lanes = payload.get("lanes") if isinstance(payload, dict) else payload
    if not isinstance(lanes, list):
        raise SafetyError("lane query state is unknown")
    paths: list[Path] = []
    for lane in lanes:
        if not isinstance(lane, dict) or "activeJob" not in lane:
            raise SafetyError("lane activity state is unknown")
        active_job = lane["activeJob"]
        if active_job is None:
            continue
        if not isinstance(active_job, str) or not active_job:
            raise SafetyError("lane activity state is unknown")
        path = _path(lane.get("cwd"))
        if path is None:
            raise SafetyError("active lane directory is unknown")
        paths.append(path)
    return paths


def _guard_paths(jobs_dir: Path = JOBS_DIR, cli: Path = LANE_CLI) -> list[Path]:
    job_paths, session_ids = _active_job_paths(jobs_dir)
    return job_paths + _session_paths(session_ids) + _lane_paths(cli)


def _git_root(candidate: Path) -> Path | None:
    try:
        result = _run(["git", "-C", str(candidate), "rev-parse", "--show-toplevel"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SafetyError("git inspection failed") from exc
    if result.returncode:
        if (candidate / ".git").exists() or (candidate / ".git").is_symlink():
            raise SafetyError("git metadata cannot be inspected")
        return None
    root = Path(result.stdout.strip()).resolve()
    if root != candidate.resolve():
        raise SafetyError("candidate is nested inside another Git worktree")
    return root


def _git_kind(candidate: Path) -> str | None:
    root = _git_root(candidate)
    if root is None:
        return None
    dotgit = root / ".git"
    if dotgit.is_file():
        return "worktree"
    if dotgit.is_dir():
        return "copy"
    raise SafetyError("git metadata is ambiguous")


def _git_is_safe(candidate: Path) -> bool:
    try:
        status = _run(["git", "-C", str(candidate), "status", "--porcelain", "--untracked-files=all"])
        upstream = _run(["git", "-C", str(candidate), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        ahead = _run(["git", "-C", str(candidate), "rev-list", "--count", "@{u}..HEAD"])
        unpublished = _run(["git", "-C", str(candidate), "rev-list", "--count", "--branches", "HEAD", "--not", "--remotes"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (
        status.returncode == 0 and not status.stdout and upstream.returncode == 0
        and bool(upstream.stdout.strip()) and ahead.returncode == 0 and ahead.stdout.strip() == "0"
        and unpublished.returncode == 0 and unpublished.stdout.strip() == "0"
    )


def _git_common_dir(candidate: Path) -> Path | None:
    try:
        result = _run(["git", "-C", str(candidate), "rev-parse", "--path-format=absolute", "--git-common-dir"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _protected_contents(candidate: Path) -> str | None:
    def fail_walk(_error: OSError) -> None:
        raise OSError("candidate contents cannot be inspected")

    try:
        for root, dirs, files in os.walk(candidate, followlinks=False, onerror=fail_walk):
            if any(fnmatch.fnmatchcase(name, pattern) for name in files for pattern in PROTECTED_FILE_GLOBS):
                return "answer_markdown"
            if Path(root) != candidate and ".git" in dirs + files:
                return "nested_git_repository"
            dirs[:] = [name for name in dirs if name != ".git" and not Path(root, name).is_symlink()]
    except OSError:
        return "contents_unknown"
    return None


def _candidate_size(path: Path) -> int | None:
    try:
        result = _run(["du", "-sk", str(path)], timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        return int(result.stdout.split()[0]) * 1024
    except (ValueError, IndexError):
        return None


def _delete_candidate(
    candidate: Path, kind: str | None, dry_run: bool, common_dir: Path | None = None
) -> str:
    if dry_run:
        return "would_remove"
    if kind == "worktree":
        common_dir = common_dir or _git_common_dir(candidate)
        if common_dir is None:
            return "remove_failed"
        git = ["git", f"--git-dir={common_dir}"]
        result = _run(git + ["worktree", "remove", str(candidate)])
        if result.returncode:
            return "remove_failed"
        _run(git + ["worktree", "prune"])
        return "removed"
    try:
        shutil.rmtree(candidate)
        return "removed"
    except OSError:
        return "remove_failed"



def _cleanup_old_logs(now: float, dry_run: bool) -> tuple[int, int, list[dict[str, object]]]:
    count = size = 0
    rows: list[dict[str, object]] = []
    if not JOBS_DIR.is_dir():
        return 0, 0, rows
    try:
        files = list(JOBS_DIR.glob("*.jsonl.gz"))
    except OSError:
        return 0, 0, rows
    for file in files:
        try:
            if file.is_symlink():
                continue
            stat = file.stat()
            if now - stat.st_mtime < 30 * DAY:
                continue
            state_file = JOBS_DIR / (file.name[:-len(".jsonl.gz")] + ".json")
            if not state_file.is_file():
                continue
            state = _job_status(json.loads(state_file.read_text(encoding="utf-8")))
            if state in {"running", "waiting_input"} or state not in TERMINAL_STATES:
                continue
            if not dry_run:
                file.unlink()
            rows.append({
                "action": "would_remove_job_log" if dry_run else "removed_job_log",
                "path": str(file), "bytes": stat.st_size, "size_gb": round(stat.st_size / 1e9, 3),
            })
            count += 1
            size += stat.st_size
        except (OSError, UnicodeError, json.JSONDecodeError, SafetyError):
            continue
    return count, size, rows


def _snapshot_report() -> int | None:
    try:
        result = _run(["tmutil", "listlocalsnapshots", "/"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return sum(1 for line in result.stdout.splitlines() if line.strip() and "Snapshots for" not in line)


def _record(entry: dict[str, object]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")


def _chrome_processes() -> list[dict[str, int]] | None:
    try:
        result = _run(["/bin/ps", "-axo", "pid=,ppid=,command="], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    root = (HOME / ".omp" / "puppeteer").resolve()
    found = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            argv = shlex.split(parts[2])
        except ValueError:
            continue
        if not argv:
            continue
        executable = _path(argv[0])
        if executable is not None and executable != root and root in executable.parents:
            found.append({"pid": pid, "ppid": ppid})
    return found


def _run_once(dry_run: bool) -> int:
    now = time.time()
    before = shutil.disk_usage(HOME).free
    report: list[dict[str, object]] = []
    candidates = discover_candidates()
    try:
        active_paths = _guard_paths()
    except SafetyError as exc:
        active_paths = []
        guard_error = str(exc)
    else:
        guard_error = ""
    for candidate in candidates:
        if _protected_path(candidate):
            report.append({"action": "kept", "reason": "protected", "path": str(candidate)})
            continue
        if guard_error:
            report.append({"action": "kept", "reason": guard_error, "path": str(candidate)})
            continue
        if any(_overlaps(candidate, active) for active in active_paths):
            report.append({"action": "kept", "reason": "active_job_or_lane_overlap", "path": str(candidate)})
            continue
        content_reason = _protected_contents(candidate)
        if content_reason:
            report.append({"action": "kept", "reason": content_reason, "path": str(candidate)})
            continue
        try:
            kind = _git_kind(candidate)
        except SafetyError as exc:
            report.append({"action": "kept", "reason": str(exc), "path": str(candidate)})
            continue
        if kind is None and candidate.parent != Path("/private/tmp").resolve():
            report.append({"action": "kept", "reason": "git_metadata_unknown", "path": str(candidate)})
            continue
        if kind and not _git_is_safe(candidate):
            report.append({"action": "kept", "reason": "git_dirty_untracked_or_unpushed_or_unknown", "path": str(candidate)})
            continue
        common_dir = None
        if kind == "worktree":
            common_dir = _git_common_dir(candidate)
            if common_dir is None:
                report.append({"action": "kept", "reason": "git_common_dir_unknown", "path": str(candidate)})
                continue
        size = _candidate_size(candidate)
        if size is None:
            report.append({"action": "kept", "reason": "candidate_size_unknown", "path": str(candidate)})
            continue
        if not dry_run:
            if kind and not _git_is_safe(candidate):
                report.append({"action": "kept", "reason": "git_state_changed_or_unknown", "path": str(candidate)})
                continue
            if candidate.is_symlink() or not candidate.is_dir():
                report.append({"action": "kept", "reason": "candidate_changed", "path": str(candidate)})
                continue
            try:
                fresh_paths = _guard_paths()
            except SafetyError as exc:
                report.append({"action": "kept", "reason": str(exc), "path": str(candidate)})
                continue
            if any(_overlaps(candidate, active) for active in fresh_paths):
                report.append({"action": "kept", "reason": "active_job_or_lane_overlap", "path": str(candidate)})
                continue
            worktrees = (HOME / ".omp" / "wt").resolve()
            if candidate.parent == worktrees and not _owner_is_orphan(candidate):
                report.append({"action": "kept", "reason": "owner_no_longer_orphaned", "path": str(candidate)})
                continue
        action = _delete_candidate(candidate, kind, dry_run, common_dir)
        report.append({"action": action, "path": str(candidate), "size_bytes": size, "size_gb": round(size / 1e9, 3)})
    if guard_error:
        log_count, log_bytes, log_rows = 0, 0, []
    else:
        log_count, log_bytes, log_rows = _cleanup_old_logs(now, dry_run)
    report.extend(log_rows)
    chrome = _chrome_processes()
    if chrome is None:
        report.append({"action": "kept", "reason": "chrome_process_query_unavailable"})
    elif chrome:
        report.extend({
            "action": "kept", "reason": "chrome_for_testing_ownership_unproven", **process,
        } for process in chrome)
    else:
        report.append({"action": "kept", "reason": "chrome_for_testing_process_not_found"})
    snapshots = _snapshot_report()
    report.append({"action": "snapshot_report", "count": snapshots})
    for item in report:
        entry = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "dry_run": dry_run, **item}
        _record(entry)
        print(json.dumps(entry, sort_keys=True))
    after = shutil.disk_usage(HOME).free
    recovered = max(0, after - before)
    candidate_bytes = sum(
        int(item.get("size_bytes", 0))
        for item in report if item.get("action") in {"removed", "would_remove"}
    )
    summary = {
        "action": "summary", "dry_run": dry_run,
        "candidate_count": sum(item.get("action") in {"removed", "would_remove"} for item in report),
        "candidate_size_gb": round(candidate_bytes / 1e9, 3),
        "job_log_size_gb": round(log_bytes / 1e9, 3),
        "free_space_change_gb": round(recovered / 1e9, 3),
        "local_snapshots": snapshots,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def run(dry_run: bool = False) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run_once(dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report planned cleanup without deleting")
    args = parser.parse_args(argv)
    try:
        return run(args.dry_run)
    except (OSError, SafetyError) as exc:
        print(f"Disk reaper stopped safely: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
