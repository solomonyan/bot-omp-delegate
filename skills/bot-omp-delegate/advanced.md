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

## Outbound webhooks

Load this reference when configuring notifications, diagnosing delivery, or implementing a receiver. Notifications are asynchronous best-effort signals; they never make a task durable or invoke a model. The bounded queue can drop events when full, shutdown can discard queued work, and retries can produce duplicates. Use job state as the source of truth and deduplicate deliveries.

### Private configuration

Configuration lives at `<state-dir>/webhooks.json` (default state directory `~/.omp-delegate`, overridden by global `--state-dir`). An absent file uses version 1 defaults: `stall_seconds: 1800`, `idle_seconds: null`, and no subscribers. Timer values are configured in the private JSON file, not through subscriber commands. The file must be a regular, owner-owned `0600` file; symlinks and insecure permissions are rejected. Writes are atomic under the private state root.

The schema is:

```json
{
  "version": 1,
  "stall_seconds": 1800,
  "idle_seconds": null,
  "subscribers": [
    {
      "id": "ops",
      "url": "https://receiver.example/events",
      "auth_header": "Authorization",
      "auth_value": "<private value>",
      "hmac_secret": "<private signing secret>",
      "signature_header": "X-OMP-Signature",
      "events": ["run.done", "run.error"],
      "lanes": ["production"]
    }
  ]
}
```

`id` is a required non-secret identifier. `url` must use HTTP(S); plain HTTP is accepted only for loopback. `auth_header`, `auth_value`, `hmac_secret`, `signature_header`, `events`, and `lanes` are optional. Signature header defaults to `X-OMP-Signature`. Events restrict delivery to listed event names. Lane entries match either lane names or lane IDs. Omitted filters match all applicable events/lanes. Subscriber IDs cannot be added twice; remove an entry before adding a replacement.

Provision via one JSON object on stdin; put no URL, header value, or signing secret in command arguments or echoed interactive input:

```sh
# The file contains one subscriber object, not the whole configuration.
# Create it privately with your secret manager/editor; use mode 0600.
omp-delegate webhook add --id ops < /private/path/subscriber.json
omp-delegate webhook list
omp-delegate webhook test
omp-delegate webhook remove ops
```

The add command reads stdin; `--id` supplies or overrides only a non-secret identifier. After add/remove, the CLI attempts runtime reload. If the daemon is absent or has no reload route, the local change remains saved and `runtime_reload` is false; the next daemon startup loads the file. List output redacts URLs and reports only whether authentication/signing are configured, plus filters and timer settings. Static errors and receipts never include credential values.

Edit timer settings directly in `webhooks.json`, preserving `0600`. Apply them with an idle-only restart (`omp-delegate daemon restart --idle-only`), or the authenticated `POST /webhooks/reload` endpoint. A failed reload leaves the last good configuration active; an invalid startup configuration disables notifications without stopping lanes. Idle-only shutdown is atomic on upgraded daemons. Before upgrading a legacy daemon without that capability, stop the coordinator from submitting new work and verify every session is settled.

`webhook test [--event daemon.up]` sends a real connectivity test to each configured subscriber, bypassing filters. It returns sanitized per-subscriber results and exits 0 when there are no subscribers or all deliveries succeed, otherwise 1. A test sends no request when no subscribers exist. The test payload uses empty lane/job/cwd/git fields.

### Event semantics

Supported event names:

| Event | Meaning |
| --- | --- |
| `daemon.up` | Daemon started serving. |
| `run.done` | A job completed. |
| `run.error` | A job errored, was aborted, or was interrupted. |
| `run.needs_input` | A newly pending dialog needs a coordinator answer; emitted once per pending dialog ID. |
| `run.stalled` | A running job produced no output for the configured threshold; skipped while waiting for input and suppressed until new output resumes. |
| `run.deadline` | A CLI waiter reached its deadline; the underlying job may continue and is not cancelled by this event. |
| `lane.idle` | Optional notification for an idle episode. |

A stall is an inactivity signal, not proof of deadlock or task failure. Polling, response acknowledgements, state reads, and heartbeat-only frames do not count as output resumption. A deadline is about the waiter, not task termination. Unassigned sessions and daemon-level events use empty lane fields. Notifications have no automatic model calls or task-policy effects.

Lifecycle event payloads use version 1 and include `event`, `lane`, `lane_id`, `job_id`, `status`, `cwd`, `branch`, `last_commit_sha`, `summary`, and offset-aware ISO timestamp `ts`; `label` is optional. Summaries are bounded to 300 characters. Git metadata is resolved by delivery workers, not lane threads, and may be empty when unavailable. Test payloads use safe generic content and empty lane/job/path/git fields.

```json
{
  "version": 1,
  "event": "run.done",
  "lane": "coding",
  "lane_id": "lane-id",
  "job_id": "job-id",
  "status": "completed",
  "cwd": "/path/to/worktree",
  "branch": "feature/ticket",
  "last_commit_sha": "commit-sha",
  "summary": "Implemented the requested change",
  "ts": "2026-10-31T12:00:00+00:00",
  "label": "OPS-42"
}
```

Git fields describe the repository when the delivery worker observes it, not an atomic completion-time snapshot. Inactivity timers use monotonic time and restart with the daemon; they do not retrospectively alert for downtime. Idle alerts exclude archived sessions and active lifecycle transitions. Configured endpoint/auth/signing values are redacted from summaries before truncation, but arbitrary project secrets are not automatically detected: paths, labels and summaries must only go to trusted receivers.

### Delivery and receiver verification

Each HTTP request is JSON (`Content-Type: application/json`) and carries `X-OMP-Delivery`, a UUID stable across retries, and `X-OMP-Event`. Configured authentication is sent using the named header/value. With an HMAC secret, the configured signature header contains `sha256=<hex>` where the digest is HMAC-SHA256 over the exact raw HTTP body bytes. Verify the signature against raw bytes before JSON parsing; compare digests in constant time and deduplicate by `X-OMP-Delivery`.

Only 2xx responses count as successful. Network failures and non-2xx statuses receive three retries after the initial attempt, using backoff of 1, 2 and 4 seconds. Each attempt has a five-second socket deadline, including trickled response headers; system DNS resolution remains governed by the OS resolver. Requests do not follow redirects or use environment proxies. Up to four workers and a 256-entry queue bound delivery work. Subscribers are queued independently, but shared-worker saturation can delay other receivers. Graceful shutdown gives queued events a one-second drain opportunity, then stops retries with a bounded worker join. Queue saturation, a slow receiver or shutdown can lose events. Retries can duplicate deliveries; there is no durable queue or replay.

Response bodies, subscriber URLs, auth values, and signing secrets are excluded from receipts and operational errors. Treat state files as sensitive regardless: the configuration contains endpoint and credential material.

## Deadline and notification behavior

`lane run` and direct `turn` accept `--label` as coordination metadata. A foreground waiter's `--max-hours` deadline triggers a best-effort `run.deadline` once per still-live job before any explicitly requested interruption; webhook failure does not alter the waiter's deadline exit status (3). Without `--interrupt-at-deadline`, the job remains running. A detached run has no foreground waiter/deadline. A label is optional, nonempty when supplied, at most 200 characters, and cannot contain control characters.

