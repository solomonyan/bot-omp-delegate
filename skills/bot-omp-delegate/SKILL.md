---
name: bot-omp-delegate
description: Run delegated coding tasks through OMP, Codex, or Kimi lanes; advanced.md covers backend configuration, webhooks, session retention, lane assignment, and backend-specific controls.
---

# Delegate work through OMP, Codex, or Kimi

Use the installed absolute path to `scripts/omp-delegate` as `$D`. The Grok Mac app does not itself prove that commands execute on this machine: use an environment where the CLI is installed and can reach the local daemon and project files. Read the README for deployment and permission details when setting up.

## Setup

1. Verify `"$D" daemon status`; if stopped, run `"$D" daemon start`. Authenticate the selected backend locally: OMP must already be logged in for `omp`, Codex CLI installed and authenticated for `codex`, or Kimi Code CLI 2.5.3 installed and authenticated for `kimi`. Existing backend configuration is reused, not copied.
2. Run `"$D" lane list` and select a lane for this worktree. If none exists, create one with `"$D" lane create --name project-task --cwd /absolute/project/path [--backend omp|codex|kimi]`; omission selects OMP. Save the returned lane id. Completion is a lane with the intended worktree and backend.
3. Establish ownership of the worktree. One writer per worktree across all backends; parallel jobs use separate worktrees/clones. The delegate checks its own active jobs, not other editors. Include commit/push permission in the task; default to neither.

Backend choice is immutable for a conversation. `session configure` does not change it; `lane new` uses the lane's current backend. Create a new lane or create a session with the desired backend and assign it to a lane to select another backend. OMP, Codex, and Kimi native histories are separate. Codex resumes its saved app-server thread after daemon restart; Kimi resumes its saved ACP session without task replay. Native provider history is not purged when delegate-owned data is deleted. Set `OMP_DELEGATE_CODEX` or `OMP_DELEGATE_KIMI` to override the executable path.

## Dispatch

For conversational webhook delivery, set the coordinator subscriber to `mode: "conversation"` and submit once with its ID:

```sh
"$D" lane run LANE_ID "EXACT TASK. Do not commit or push." \
  --detach --coordinator-subscriber-id COORDINATOR_ID --max-hours 6
```

The recorded owner alone receives `run.done`, `run.error`, and `run.needs_input`; owner selection overrides event/lane filters. Ownerless jobs remain inspectable with `read`/`wait` and send no conversational webhook. The subscriber's monitor mode is not suitable for conversational delivery.

Grok receives a question on `run.needs_input`, answers through the existing `answer` command, and receives the full final response on settlement. Continue follow-up work in the same lane/session. The `summary` field is only a bounded preview; use the full message/input fields. Receivers deduplicate by stable `event_id` before invoking Grok; `X-OMP-Delivery` is stable across HTTP retries but delivery is not exactly-once.

Use detached job plus webhook as the single conversational wake path. Do not also run a foreground waiter that wakes Grok. Use `read`/`wait` for inspection or recovery, not progress notifications. Operational notifications are routed separately to monitor subscribers and must not wake the model. A waiter deadline does not cancel the job unless interruption was explicitly selected.

## Continue or release

Use `read`, `wait`, `answer`, and `interrupt` with the returned job id; `steer` is available only on supported backends. After settlement, the assigned conversation remains available; another lane run lazily resumes it. Close only when the worker is idle:

```sh
"$D" lane close LANE_ID
```

Busy close fails safely. `lane close LANE_ID --interrupt` explicitly requests interruption before shutdown; OMP also withdraws queued instructions. Interruption is not a hard stop for external effects. Use `lane new LANE_ID` only for unrelated work requiring a fresh conversation; it preserves the prior history. Never resubmit a task to recover a lost waiter: read/wait on its original job.

OMP completes after session settlement; Codex completes on its owning turn's terminal event, not necessarily the settlement of detached child work. Kimi completes on its owning ACP prompt response. After restart, unfinished work is interrupted, never replayed automatically. Inspect actual changes and saved history before resuming.

## Boundaries

One lane coordinates a worktree; it does not authorize access. Labels are not security boundaries, and clients sharing the daemon token share control. Kimi ACP runs in yolo mode and auto-approves everything; OMP and Codex retain their backend-specific approval policies. Waiters make no product decisions or permission grants. The delegate is not an isolation sandbox; Codex's own sandbox policy applies separately. Interruption/shutdown may leave descendants, detached tools, or external effects; do not claim full process-tree cleanup.

Load [`advanced.md`](advanced.md) for webhook configuration/receivers, or for session archive/deletion/configuration, lane assignment/removal, and subagent procedures.
