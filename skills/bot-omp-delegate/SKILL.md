---
name: bot-omp-delegate
description: Run delegated coding tasks through OMP lanes; advanced.md covers archive, deletion, configuration, assignment, and subagents.
---

# Delegate work to OMP

Use the installed absolute path to `scripts/omp-delegate` as `$D`. The Grok Mac app does not itself prove that commands execute on this machine: use an environment where the CLI is installed and can reach the local daemon, OMP, and project files. Read the README for deployment and permission details when setting up.

## Setup

1. Verify `omp --help` and `"$D" daemon status`. If stopped, run `"$D" daemon start`; completion is authenticated health success. OMP must already be logged in on this computer. Existing setup is reused, not copied.
2. Run `"$D" lane list` and select a lane for this worktree. If none exists, run `"$D" lane create --name project-task --cwd /absolute/project/path`. Save the returned lane id. Completion is a lane with the intended worktree.
3. Establish ownership of the worktree. One writer per worktree; parallel jobs use separate worktrees/clones. The delegate checks its own active jobs, not other editors. Include commit/push permission in the task; default to neither.

## Dispatch

Assign exactly one low-effort waiter subagent. Give it the absolute delegate path, lane id, exact prompt, duration, and whether deadline interruption is authorized.

```sh
"$D" lane run LANE_ID "EXACT TASK. Do not commit or push." --max-hours 6
```

Use a foreground Shell call with the platform's long blocking allowance. `lane run` returns a job id before waiting; running means admitted, not complete. If the call returns a shell handle, keep waiting on that same handle. Submit once only. The default approval tier is always-ask; waiters escalate pending UI requests to the coordinator, which resolves or obtains owner authorization before answering. Load `advanced.md` for the exact answer command/forms.

The main bot stays free and resumes on the waiter report. If a question appears, escalate it while preserving the foreground wait. When the task settles, report the lane, job id, terminal status, error, concise final message, unresolved input, and verification limits.

## Continue or release

Use `read`, `wait`, `steer`, `answer`, and `interrupt` with the returned job id. After settlement, the assigned conversation remains available; another lane run lazily resumes it. Close only when the worker is idle:

```sh
"$D" lane close LANE_ID
```

Busy close fails safely. `lane close LANE_ID --interrupt` explicitly withdraws queued instructions and interrupts work before shutdown. Interruption is not a hard stop for external effects. Use `lane new LANE_ID` only for unrelated work requiring a fresh conversation; it preserves the prior history. Never resubmit a task to recover a lost waiter: read/wait on its original job.

Only settled work is complete; a yielded agent may still have background jobs. After restart, unfinished work is interrupted, never replayed automatically. Inspect actual changes and saved history before resuming.

## Boundaries

One lane coordinates a worktree; it does not authorize access. Labels are not security boundaries, and clients sharing the daemon token share control. The owner controls approval policy. Waiters make no product decisions or permission grants. OMP is not an OS sandbox. Interruption/shutdown may leave descendants, detached tools, or external effects; do not claim full process-tree cleanup.

Load [`advanced.md`](advanced.md) only for session archive/deletion/configuration, lane assignment/removal, or subagent procedures.
