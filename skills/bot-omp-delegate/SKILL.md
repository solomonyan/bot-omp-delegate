---
name: bot-omp-delegate
description: Delegate coding or long-running work from Grok Bot to installed OMP; reuse named sessions, wait without polling model turns, answer worker questions, steer or interrupt jobs.
---

# Delegate work to OMP

Use the installed absolute path to `scripts/omp-delegate` as `$D`. Read the project's README for deployment, permissions and environment configuration when setting up a new computer.

## Setup

1. Verify `omp --help` and `"$D" daemon status`. If stopped, run `"$D" daemon start`; completion is authenticated health success. OMP must already be logged in on this computer. Existing setup is reused, not copied.
2. Run `"$D" session list`. Reuse one session per bot × project × task type. When none fits, create it with `"$D" session create --name grok-project-tasktype --cwd /absolute/project/path`. Save its returned id. Completion is a matching named session with the correct cwd/options.
3. Establish ownership of the worktree. One writer per worktree; parallel jobs use separate worktrees/clones. The delegate checks its own active jobs, not other editors or agents. Include commit/push permission in the task; default to neither.

## Dispatch

Assign exactly one low-effort waiter subagent to the job. Give it the absolute delegate path, session id, exact prompt, duration and deadline-interrupt permission.

The waiter runs:

```sh
"$D" turn SESSION_ID "EXACT TASK. Do not commit or push." --max-hours 6
```

Use a **foreground** Shell call with the platform's long blocking allowance (for environments supporting it, `block_until_ms: 760000`). The initial JSON record establishes the job id; `running` is admission, not proof of completion. If the call returns a shell handle while still running, use back-to-back foreground `AwaitShell` calls on that same handle. Keep waiting without commentary or sleeps between calls. Submit the turn once only.

The main bot stays free and resumes on the waiter report. A detached command alone does not keep a cloud computer awake. This workflow relies on the host platform counting foreground waits as activity; verify that once per deployment rather than assuming it.

If the shell connection is lost, recover with `"$D" read JOB_ID` and `"$D" wait JOB_ID --max-hours HOURS_REMAINING`. Never resubmit the prompt to recover a lost waiter. Completion is terminal JSON after OMP settles, not merely an assistant message or agent yield. Exit 0 means completed; 1 error; 2 aborted/interrupted; 3 waiter deadline. Exit 3 leaves work running unless `--interrupt-at-deadline` was explicitly authorized.

## Questions and approvals

On `waiting_input`, notify the coordinator immediately with the job id and `pendingInput` from stderr or `"$D" read JOB_ID`, while preserving the foreground wait. The coordinator resolves the question or gets owner approval, then sends the exact UI response from a separate Shell call:

```sh
"$D" answer JOB_ID --json '{"id":"UI_ID","confirmed":false}'
"$D" answer JOB_ID --json '{"id":"UI_ID","answers":[{"id":"QUESTION_ID","selectedOptions":["EXACT LABEL"]}]}'
```

Waiters make no product decisions and grant no permissions. The default approval tier is always-ask. Broader `write`/`yolo` modes require explicit owner authorization when creating the session. OMP is not an OS sandbox. Respect the request timeout: OMP may apply its own default answers when a dialog expires.

## Steering, interruption and inspection

```sh
"$D" steer JOB_ID "Additional instructions"
"$D" interrupt JOB_ID
"$D" events JOB_ID --after 0
"$D" session compact SESSION_ID
```

Steer only a running job. Interrupt aborts and withdraws queued instructions; it is not a hard stop of all external effects. Inspect the actual worktree before resuming. Event pagination returns `next`; use it as the next `--after` cursor. Compact only an idle session.

After completion the waiter reports: session id, job id, terminal status, error if any, finalMessage (trimmed), and any unresolved input or verification limit. It does not edit files, commit, push, or start another job. The coordinator reviews the actual changes and verification evidence before reporting success or dispatching the next turn.

After a crash, unfinished jobs are marked interrupted on restart; graceful shutdown can record aborted instead. Prompts are never replayed automatically. Inspect saved OMP transcripts and real changes before starting a new turn. Keep auth/token files private; do not paste credentials or export your Mac login to a cloud computer.
