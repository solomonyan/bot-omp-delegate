# Advanced lifecycle

Load this reference only for session retention/configuration, lane assignment/removal, or subagent control. Routine work uses lane create/list/show/run/close/new and the returned job ID.

## Questions and approvals

For a pending UI request, the default approval tier is always-ask. The coordinator resolves the request and obtains owner approval when required; a waiter never chooses an answer or grants permission. Use the exact request ID and values/options from `pendingInput`:

```sh
omp-delegate answer JOB_ID --json '{"id":"UI_ID","confirmed":false}'
omp-delegate answer JOB_ID --json '{"id":"UI_ID","answers":[{"id":"QUESTION_ID","selectedOptions":["EXACT LABEL"]}]}'
```

## Session lifecycle

- `session show ID` inspects a snapshot; `session list --archived` includes archived records.

- `session open ID` optionally prewarms a worker. Closed, available sessions reopen lazily when dispatched.
- `session close ID [--interrupt]` closes a worker but keeps its conversation. Without explicit interruption, busy close conflicts.
- `session archive ID` stops a settled worker and rejects dispatch while retaining history. `session unarchive ID` makes it available again.
- `session configure ID [worker flags]` merges options while settled and closes the idle worker so the next open applies them without erasing conversation history.

Closed and archived are distinct: closed means available for lazy resumption; archived means dispatch is disabled until unarchived.

## Assignment and removal

`lane assign ID --session SID --expected-revision N` adopts an existing session only when settled, same-worktree, and not assigned elsewhere. Revisions reject stale updates. `lane delete ID [--expected-revision N]` removes only an idle/stopped lane registration; its sessions, jobs, and history remain. A lane label is coordination metadata, never an authorization boundary.

## Confirmed deletion

`session delete ID --confirm-id ID` requires explicit ID confirmation. It is restricted to archived, stopped sessions not assigned to any lane. The daemon purges only its owned session/job/event/log data and preserves minimal identity tombstones needed by lane history. It does not remove project files, worktrees, branches, provider-side history, credentials, or unrelated state. Filesystem removal is not secure erasure and cannot retract backups or exports. Never use deletion as routine task cleanup.

## Subagents

Use the parent job handle and child ID, keeping control bound to that job/generation:

```sh
omp-delegate agent list JOB_ID
omp-delegate agent read JOB_ID AGENT_ID --from-byte 0
omp-delegate agent steer JOB_ID AGENT_ID "Additional direction"
omp-delegate agent cancel JOB_ID AGENT_ID
```

Cancellation is an OMP control request, not an OS-level hard kill. Descendant cleanup depends on OMP's supported behavior; detached tools, external side effects, and unrelated processes may continue. A stale parent job must not be redirected to a successor worker. Use job events and full session history only when the investigation requires them.
