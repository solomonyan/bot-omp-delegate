---
name: bot-omp-delegate
description: Run delegated coding tasks through OMP lanes; advanced.md covers webhooks, session retention/configuration, lane assignment/removal, and subagents.
---

# Delegate work to OMP

Use the installed absolute path to `scripts/omp-delegate` as `$D`. The Grok Mac app does not itself prove that commands execute on this machine: use an environment where the CLI is installed and can reach the local daemon, OMP, and project files. Read the README for deployment and permission details when setting up.

## Setup

1. Verify `omp --help` and `"$D" daemon status`. If stopped, run `"$D" daemon start`; completion is authenticated health success. OMP must already be logged in on this computer. Existing setup is reused, not copied.
2. Run `"$D" lane list` and select a lane for this worktree. If none exists, run `"$D" lane create --name project-task --cwd /absolute/project/path`. Save the returned lane id. Completion is a lane with the intended worktree.
3. Establish ownership of the worktree. One writer per worktree; parallel jobs use separate worktrees/clones. The delegate checks its own active jobs, not other editors. Include commit/push permission in the task; default to neither.

## Dispatch

For conversational webhook delivery, set the coordinator subscriber to `mode: "conversation"` and submit once with its ID:

```sh
"$D" lane run LANE_ID "EXACT TASK. Do not commit or push." \
  --detach --coordinator-subscriber-id COORDINATOR_ID --max-hours 6
```

The owner alone receives `run.done`, `run.error`, `run.needs_input` (after a 30 s grace, only if still pending) and `run.stalled`. The owner is the subscriber the job's `omp-caller:<name>` label maps to in `caller_routes`, else the recorded coordinator; owner selection overrides event/lane filters. Every other event stays in the daemon's local filtered-events log. Ownerless jobs remain inspectable with `read`/`wait` and send no webhook. The subscriber's monitor mode is not suitable for conversational delivery.

Grok receives a question on `run.needs_input`, answers through the existing `answer` command, and receives the full final response on settlement. Continue follow-up work in the same lane/session. The `summary` field is only a bounded preview; use the full message/input fields. Receivers deduplicate by stable `event_id` before invoking Grok; `X-OMP-Delivery` is stable across HTTP retries but delivery is not exactly-once.

Use detached job plus webhook as the single conversational wake path. Do not also run a foreground waiter that wakes Grok. Use `read`/`wait` for inspection or recovery, not progress notifications. Operational notifications are routed separately to monitor subscribers and must not wake the model. A waiter deadline does not cancel the job unless interruption was explicitly selected.

## Continue or release

Use `read`, `wait`, `steer`, `answer`, and `interrupt` with the returned job id. After settlement, the assigned conversation remains available; another lane run lazily resumes it. Close only when the worker is idle:

```sh
"$D" lane close LANE_ID
```

Busy close fails safely. `lane close LANE_ID --interrupt` explicitly withdraws queued instructions and interrupts work before shutdown. Interruption is not a hard stop for external effects. Use `lane new LANE_ID` only for unrelated work requiring a fresh conversation; it preserves the prior history. Never resubmit a task to recover a lost waiter: read/wait on its original job.

Only settled work is complete; a yielded agent may still have background jobs. After restart, unfinished work is interrupted, never replayed automatically. Inspect actual changes and saved history before resuming.

## Boundaries

One lane coordinates a worktree; it does not authorize access. Labels are not security boundaries, and clients sharing the daemon token share control. The owner controls approval policy. Waiters make no product decisions or permission grants. OMP is not an OS sandbox. Interruption/shutdown may leave descendants, detached tools, or external effects; do not claim full process-tree cleanup.

Load [`advanced.md`](advanced.md) for webhook configuration/receivers, or for session archive/deletion/configuration, lane assignment/removal, and subagent procedures.
