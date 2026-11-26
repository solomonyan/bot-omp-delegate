# Advanced lifecycle

Load this reference for webhook configuration/receivers, session retention/configuration, lane assignment/removal, or subagent control. Routine work uses lane create/list/show/run/close/new and the returned job ID.

## Backend configuration

Choose `--backend omp|codex|kimi` on `lane create` or `session create`; omission selects OMP. Backend selection is stored in `options.backend`. `session configure` may change supported worker options while settled, but cannot switch backend; create a new conversation to use another backend. OMP, Codex, and Kimi native histories are not interchangeable.

Codex requires an installed, authenticated Codex CLI and uses its persistent headless `codex app-server`, not `codex exec`. Set `OMP_DELEGATE_CODEX` to override the executable. Codex resumes its saved native thread after daemon restart without replaying the task. The thread ID is private delegate session state; purging delegate data does not purge Codex history.

Codex supports `--model`, `--thinking` as turn effort, and repeated `--add-dir` as writable roots. It rejects `--config` file overlays, `--tools`, and `--profile`: the installed Codex app-server does not accept named profiles. Native Codex configuration and inherited environment remain authoritative. The daemon stores every session as `approval_mode: yolo`, which Codex receives as `never` approval plus `danger-full-access` (full-auto); any other requested mode is overridden.

Kimi requires installed, authenticated Kimi Code CLI 2.5.3 and uses persistent `kimi acp`. Set `OMP_DELEGATE_KIMI` to override the executable. The worker inherits the operator's Kimi environment, `KIMI_CODE_HOME`, login, configuration, skills, and target-project instructions; credentials are not copied. A private session identity lets it resume the native Kimi conversation after daemon restart without replaying prompts. Deleting delegate state does not purge Kimi-managed native history.

Kimi supports native model aliases through `--model`, `--thinking low|high|max`, and repeated `--add-dir` roots at native session creation. It rejects `--config`, `--profile`, and `--tools`; Kimi roots cannot be changed by session configure, so create a new conversation to change them. The daemon enforces `approval_mode: yolo` at every startup/resume through ACP `session/set_config_option` mode `yolo`, meaning auto-approve everything. Do not use global CLI flags as a substitute; the ACP session mode is the enforced policy.

The detached-submit, owner-routed webhook, question/answer, and same-session follow-up workflow is shared. Conversation events (`run.done`, `run.error`, `run.needs_input`) go to the recorded coordinator subscriber; operational events remain monitor-only. Adding Kimi does not change delivery routing.

Codex and Kimi are not feature-equivalent to OMP's native child controls or completion behavior. OMP `agent list/read/steer/cancel` and native parent continuation are OMP-specific. Kimi explicitly rejects steer (ACP has no safe mid-turn steer), manual compact (the advertised command is only an explanatory notice), and direct child controls. Do not send a second prompt as a steer, fake compact success, or invent child results. Native automatic context management remains Kimi-owned.

Kimi permission `select` requests expose the full native options. Answer with the exact native `optionId` in `value`, not the label; never implicitly choose `allow_always`. A `confirmed` boolean is accepted only when exactly one `allow_once` or `reject_once` choice maps unambiguously to it. Elicitation is surfaced as `ask`: provide every required question ID in `answers`, with exact `selectedOptions` values for choices/multi-select or `text` for free-text fields. A `value` object keyed by form field IDs is also accepted. Invalid or partial answers remain pending; unknown native requests fail explicitly.

## Questions and approvals

Every worker runs yolo; the daemon stores any requested `approval_mode` as `yolo` and logs one stderr line for a non-yolo request. On OMP that is `--approval-mode yolo`; on Codex it is `never` approval plus `danger-full-access`. A waiter or coordinator never grants permission on the owner's behalf. Inspect the full pending request and use its exact request ID and values/options:

```sh
omp-delegate answer JOB_ID --json '{"id":"UI_ID","confirmed":false}'
omp-delegate answer JOB_ID --json '{"id":"UI_ID","answers":[{"id":"QUESTION_ID","selectedOptions":["EXACT LABEL"]}]}'
```

Do not suppress unresolved approval dialogs. OMP's configured native permission policy remains authoritative; Codex and Kimi requests use their native response workflows.

## Session lifecycle

- `session show ID` inspects a snapshot; `session list --archived` includes archived records.

- `session open ID` optionally prewarms a worker. Closed, available sessions reopen lazily when dispatched.
- `session close ID [--interrupt]` closes a worker but keeps its conversation. Without explicit interruption, busy close conflicts.
- `session archive ID` stops a settled worker and rejects dispatch while retaining history. `session unarchive ID` makes it available again.
- `session configure ID [worker flags]` merges supported options while settled and closes the idle worker so the next open applies them without erasing conversation history. It does not change the session backend.

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

Cancellation is an OMP control request, not an OS-level hard kill. These native child controls are not available for Codex unless separately implemented; report unsupported operations rather than returning invented agent data. OMP descendant cleanup depends on its supported behavior; detached tools, external side effects, and unrelated processes may continue. A stale parent job must not be redirected to a successor worker. Use job events and full session history only when the investigation requires them.

Native `task` results wake their owning OMP parent session in-process. A `prompt_result` with `sessionSettled: false` is a yield, not final job completion: the delegate retains the same worker/job and worktree ownership through child-result delivery and resumed parent output, then settles on `session_settled`. Do not relay the result as another prompt or create a second parent session. Reopening with `--continue` is separate conversation persistence, not the result wake path.

## Outbound webhooks

Load this reference when configuring notifications, diagnosing delivery, or implementing a receiver. Notifications are asynchronous best-effort signals; they never make a task durable or invoke a model. The bounded queue can drop events when full, shutdown can discard queued work, and retries can produce duplicates. Use job state as the source of truth and deduplicate deliveries.

### Conversational ownership and monitoring

Set subscriber `mode` to `conversation` or `monitor`; omitted mode defaults to `monitor` for observer compatibility. Use `conversation` for the Grok coordinator receiver, so it is excluded from operational notifications.

Pass `--coordinator-subscriber-id ID` with `lane run` or direct `turn`; HTTP dispatch uses `coordinator_subscriber_id`. The ID must exist in webhook configuration and is recorded on the job. A `caller_routes` entry for the job's label (for example `"omp-caller:karen": "karen-omp-events"`) overrides it; an unmapped label, or one mapped to a subscriber that no longer exists, falls back to the recorded ID. That owner receives `run.done`, `run.error`, `run.needs_input` and `run.stalled`, with owner selection overriding its event/lane filters. A recorded owner may be either mode. Ownerless jobs have no webhook and remain available through `read`/`wait`.

Only `run.done`, `run.error`, `run.needs_input` and `run.stalled` are ever posted. Each goes to the subscriber that the job's label maps to in `caller_routes`, or, for an unmapped label, to the job's recorded `coordinator_subscriber_id`; owner selection overrides that subscriber's event/lane filters. `run.stalled` also reaches `monitor` subscribers whose filters match. Ownerless jobs remain available through `read`/`wait` and produce no webhook. Every other event (`run.deadline`, `lane.idle`, `daemon.up`) and every unrouted event is appended only to the private local log `<state-dir>/logs/events-filtered.jsonl` (redacted, `0600`, rotated to `.1` at 1 MiB). Notifications do not invoke a model.

Grok submits once with the coordinator ID, handles `run.needs_input` through the existing `answer` path, receives the final full response, and continues in the same session. Use a detached job plus webhook as the conversational path; use `read`/`wait` for inspection and recovery rather than a second foreground wake path. Do not wake Grok for progress or operational events.

Version 1 notifications include `channel` (`conversation` or `monitor`), `session_id`, `job_id`, and stable business `event_id`. Terminal events include the full `message`; input events include `input_id` and the full input request. `summary` remains a bounded diagnostic preview, not a substitute for message/input content. Endpoint, auth, and HMAC configured values are recursively redacted from user-visible string values. Receivers deduplicate by `event_id` before invoking Grok. `X-OMP-Delivery` is stable across retries; retries can duplicate HTTP requests and there is no exactly-once guarantee.

### Private configuration

Configuration lives at `<state-dir>/webhooks.json` (default state directory `~/.omp-delegate`, overridden by global `--state-dir`). An absent file uses version 1 defaults: `stall_seconds: 1800`, `idle_seconds: null`, and no subscribers. Timer values are configured in the private JSON file, not through subscriber commands. The file must be a regular, owner-owned `0600` file; symlinks and insecure permissions are rejected. Writes are atomic under the private state root.

Optional `caller_routes` maps a job label to a subscriber ID (never a URL; endpoints and credentials stay in the subscriber entry). Older daemons reject this key, so add it to `webhooks.json` only after upgrading.

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
      "events": ["run.stalled", "run.deadline"],
      "lanes": ["production"],
      "mode": "monitor"
    }
  ]
}
```

`id` is a required non-secret identifier. `url` must use HTTP(S); plain HTTP is accepted only for loopback. `auth_header`, `auth_value`, `hmac_secret`, `signature_header`, `events`, `lanes`, and `mode` are optional. `mode` accepts `conversation` or `monitor` and defaults to `monitor`. Signature header defaults to `X-OMP-Signature`. Events restrict operational notifications to listed event names; lane entries match either lane names or lane IDs. Operational notifications only reach monitor subscribers; conversational events route only to the recorded job owner, regardless of that owner's filters.

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
| `run.needs_input` | A newly pending input request; emitted once per pending dialog ID. It is delivered only to the recorded coordinator owner. |
| `run.stalled` | A running job produced no output for the configured threshold; skipped while waiting for input and suppressed until new output resumes. |
| `run.deadline` | A CLI waiter reached its deadline; the underlying job may continue and is not cancelled by this event. |
| `lane.idle` | Optional notification for an idle episode. |

Only `run.done`, `run.error`, `run.needs_input` and `run.stalled` are posted, to the routed owner (and `run.stalled` also to matching monitor subscribers). `run.deadline`, `lane.idle` and `daemon.up` are recorded in the local filtered-events log only; `webhook test` still posts directly to every subscriber. A stall is an inactivity signal, not proof of deadlock or task failure. A deadline is about the waiter, not task termination. Notifications have no automatic model calls or task-policy effects.

Version 1 payloads include `channel`, `session_id`, `event_id`, `event`, `lane`, `lane_id`, `job_id`, `status`, `cwd`, `branch`, `last_commit_sha`, `summary`, and offset-aware ISO timestamp `ts`; `label` is optional. Terminal conversational events include full `message`; `run.needs_input` includes `input_id` and full input request. Summary is bounded to 300 characters and is only a diagnostic preview. Git metadata is resolved by delivery workers and may be empty when unavailable. Configured endpoint/auth/HMAC values are recursively redacted from user-visible string values, not structural keys or identifiers.

```json
{
  "version": 1,
  "channel": "conversation",
  "session_id": "session-id",
  "event_id": "stable-business-event-id",
  "event": "run.done",
  "lane": "coding",
  "lane_id": "lane-id",
  "job_id": "job-id",
  "status": "completed",
  "cwd": "/path/to/worktree",
  "branch": "feature/ticket",
  "last_commit_sha": "commit-sha",
  "summary": "Bounded diagnostic preview",
  "message": "Full final response",
  "ts": "2026-10-31T12:00:00+00:00",
  "label": "OPS-42"
}
```

Git fields describe the repository when the delivery worker observes it, not an atomic completion-time snapshot. Inactivity timers use monotonic time and restart with the daemon; they do not retrospectively alert for downtime. Idle alerts exclude archived sessions and active lifecycle transitions. Configured endpoint/auth/signing values are recursively redacted from user-visible payload string values, but arbitrary project secrets are not automatically detected: paths, labels and summaries must only go to trusted receivers.

### Delivery and receiver verification

Each HTTP request is JSON (`Content-Type: application/json`) and carries `X-OMP-Delivery`, a UUID stable across retries, and `X-OMP-Event`. Configured authentication is sent using the named header/value. With an HMAC secret, the configured signature header contains `sha256=<hex>` where the digest is HMAC-SHA256 over the exact raw HTTP body bytes. Verify the signature against raw bytes before JSON parsing; compare digests in constant time and deduplicate conversational turns by `event_id` before invoking Grok.

Only 2xx responses count as successful. Network failures and non-2xx statuses receive three retries after the initial attempt, using backoff of 1, 2 and 4 seconds. Each attempt has a five-second socket deadline, including trickled response headers; system DNS resolution remains governed by the OS resolver. Requests do not follow redirects or use environment proxies. Up to four workers and a 256-entry queue bound delivery work. Subscribers are queued independently, but shared-worker saturation can delay other receivers. Graceful shutdown gives queued events a one-second drain opportunity, then stops retries with a bounded worker join. Queue saturation, a slow receiver or shutdown can lose events. Retries can duplicate deliveries; there is no durable queue or replay.

Response bodies, subscriber URLs, auth values, and signing secrets are excluded from receipts and operational errors. Treat state files as sensitive regardless: the configuration contains endpoint and credential material.

## Deadline and notification behavior

`lane run` and direct `turn` accept `--label` as coordination metadata. A foreground waiter's `--max-hours` deadline records a `run.deadline` in the local filtered-events log once per still-live job before any explicitly requested interruption; webhook failure does not alter the waiter's deadline exit status (3). Without `--interrupt-at-deadline`, the job remains running. A detached run has no foreground waiter/deadline. A label is optional, nonempty when supplied, at most 200 characters, and cannot contain control characters.

