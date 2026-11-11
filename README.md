# bot-omp-delegate

Let Grok Bot delegate work to a resident local OMP or Codex worker, retain project context, and steer or interrupt work without repeated model turns.
Each operator uses their own installed CLI, login, configuration and project files. Provider credentials are not bundled with the project.


## Outbound webhook notifications

Webhooks are best-effort asynchronous notifications, not a durable queue. Configure subscribers in the private `webhooks.json` under the delegate state directory; protect endpoint/auth/HMAC values and send only to trusted receivers. `events` and `lanes` filter operational notifications. Subscriber `mode` is `conversation` or `monitor` and defaults to `monitor` for existing observer compatibility.

Only `run.done`, `run.error`, `run.needs_input` and `run.stalled` are ever posted. Each goes to the subscriber that the job's label maps to in `caller_routes`, or, for an unmapped label, to the job's recorded `coordinator_subscriber_id`; owner selection overrides that subscriber's event/lane filters. `run.stalled` also reaches `monitor` subscribers whose filters match. Ownerless jobs remain available through `read`/`wait` and produce no webhook. Every other event (`run.deadline`, `lane.idle`, `daemon.up`) and every unrouted event is appended only to the private local log `<state-dir>/logs/events-filtered.jsonl` (redacted, `0600`, rotated to `.1` at 1 MiB). Notifications do not invoke a model.

Version 1 payloads include `channel` (`conversation` or `monitor`), `session_id`, `job_id`, and stable business `event_id`; terminal responses carry the full `message`, and questions carry `input_id` plus the full input request. The bounded `summary` is a diagnostic preview, not the response. Configured endpoint/auth/HMAC values are recursively redacted from user-visible string values, not structural keys or identifiers. `X-OMP-Delivery` remains stable across HTTP retries. Receivers should deduplicate by `event_id` before invoking Grok; HTTP delivery is best-effort and is not exactly-once. Verify configured HMAC signatures against raw request bytes before parsing.

Grok submits once with its coordinator subscriber ID, receives questions and answers them through the existing `answer` path, then receives the full final response. Continue follow-up work in the same session. Use a detached job plus webhook as the primary conversational path; `read`/`wait` are for inspection and recovery. Do not keep a foreground waiter and webhook both waking Grok, and do not wake the model for progress or operational events.

The daemon's delivery queue is bounded; shutdown or queue saturation can lose notifications, and receivers should use job state for recovery. For configuration schema, event semantics, and delivery limits, see [`skills/bot-omp-delegate/advanced.md`](skills/bot-omp-delegate/advanced.md).


## Requirements and setup

- macOS or Linux; Python 3.11+; install and authenticate OMP, Codex CLI, or both for the backend you select.
- No Python packages or JavaScript dependencies to install.
- OMP requires RPC v2 negotiation, `prompt_result`, `session_settled`, and `abort_and_restore_queue`.
- Codex uses the persistent headless `codex app-server` protocol, not one-shot `codex exec`. Verified with Codex CLI 0.167.1; see [backend configuration](#backend-selection-and-configuration).

```sh
git clone https://github.com/solomonyan/bot-omp-delegate.git
cd bot-omp-delegate
export PATH="$PWD/scripts:$PATH"
omp-delegate --help
omp-delegate daemon start
omp-delegate daemon status
```

Keep this directory in place; add the absolute `scripts` directory to your shell PATH for later use. In a supervisor, run `omp-delegate serve` in the foreground instead of `daemon start`. The detached daemon survives its launching shell, not a reboot. There is no automatic job replay or watchdog restart.

Optional ticket-file dispatch uses `omp-dispatch LANE_ID /path/to/ticket.txt [max-hours]`.
The ticket must contain a non-empty `Must-have:` line. Set
`OMP_COORDINATOR_SUBSCRIBER_ID` to the job's webhook owner; `--caller NAME` adds
a caller label, and `--dry-run` previews the detached submission without dispatching.
The wrapper uses the sibling `omp-delegate` executable and never changes the session's
approval mode. Subscriber discovery and local monitoring
belong in private adapters, not this shared dispatcher.

## Use with an existing project

Grok provides the task; the delegate starts the selected installed CLI in the target project's working directory. The Grok Mac app alone does not establish that it can execute local commands: use an executor with the CLI installed and access to the daemon and project files. Each backend uses its own login and global/project instructions to perform work and return questions or results.

```mermaid
flowchart LR
    A[Grok Bot] -->|Task| B[Local delegate daemon]
    B -->|Backend-specific stdio RPC| C[Persistent OMP or Codex worker]
    D[Global OMP setup] -->|OMP only| C
    F[Codex login and config] -->|Codex only| C
    E[Target project config and instructions] --> C
    C -->|Questions and settled result| B
    B -->|Owner-routed webhook| A
```

## Routine lane workflow

Create one lane per existing worktree. Lane creation records a fresh conversation without starting a worker:

```sh
PROJECT=/absolute/path/to/worktree
omp-delegate lane create --name project --cwd "$PROJECT" --backend codex
omp-delegate lane list
omp-delegate lane show LANE_ID
omp-delegate lane run LANE_ID "Investigate and implement the requested change"
```

`lane run` returns a job handle, then waits in the foreground by default. Keep its ID for `read`, `wait`, `steer`, `answer`, or `interrupt`; use one waiter per job. `--detach` returns immediately. A closed available conversation automatically reopens on the next run. Completion retains the conversation; prompts are never replayed after interruption or daemon restart.

Foreground `lane run` and `turn` write an initial job record, then a final record: stdout is JSONL, not one combined JSON document. `wait` returns the final record. Exit codes are 0 completed, 1 error, 2 aborted/interrupted, and 3 waiter deadline. A deadline leaves work running unless `--interrupt-at-deadline` was explicitly selected. Reconnect using the original job handle; do not resubmit the task.

```sh
omp-delegate lane close LANE_ID
omp-delegate lane close LANE_ID --interrupt
omp-delegate lane new LANE_ID
```

Default close only closes an idle settled worker. Close preserves the conversation; `lane new` explicitly creates a fresh conversation after settlement and preserves old history. Use separate worktrees for parallel jobs.

### Backend selection and configuration

Select a backend when creating a conversation; omission means `omp`:

```sh
omp-delegate lane create --name project --cwd "$PROJECT" --backend codex
omp-delegate session create --name project-task --cwd "$PROJECT" --backend omp
```

Backend choice is saved in `options.backend` and exposed in session/lane snapshots; `session configure` cannot change it. `lane new` creates a fresh conversation on that lane's existing backend. To choose another backend, create a new lane or create a session with the desired backend and assign it; selecting another backend starts its own native history, not a continuation of the prior backend's history.

Codex requires an installed, authenticated Codex CLI. The worker uses its persistent headless app-server interface and resumes the saved native thread after daemon restart without replaying the task. Its thread identity is private delegate session state; deleting delegate-owned data does not purge Codex's native history. `OMP_DELEGATE_CODEX` selects the Codex executable (default `codex`); `OMP_DELEGATE_OMP` continues to select OMP.

For Codex, `--model`, `--thinking` (turn effort), and repeated `--add-dir` (writable roots) are supported. `--config` file overlays, `--tools`, and `--profile` are rejected; the installed Codex app-server does not accept named profiles. Its inherited environment and native configuration remain authoritative. The daemon stores every session as `approval_mode: yolo`, which Codex receives as `never` approval plus `danger-full-access` (full-auto); any other requested mode is overridden.

The Grok detached-submit, owner-routed webhook, question/answer and same-session follow-up workflow is unchanged across backends. Both backends share the same worktree exclusion. Codex does not provide feature parity with OMP-native child controls or completion behavior; OMP subagent controls and native parent continuation remain OMP-specific.

Codex stores `codex-thread.json` privately in the delegate session directory. Prewarm-only threads have no native rollout and may receive a new empty native identity on reopen. Before the first prompt is sent, the delegate durably records the dispatch attempt; subsequent resume failures are errors, never a reset or automatic prompt replay. Command/file approvals and user-input questions are supported; other native server requests receive explicit unsupported-operation errors.

### Native parent continuation

OMP delivers native `task` results into the same owning parent session and triggers its next turn in-process. The delegate keeps the worker, parent job and worktree ownership active when `prompt_result` has `sessionSettled: false`; it records the resumed parent's final message and finishes only after `session_settled`. Child completion alone is not a top-level completion webhook. No relay bot, second parent session or extra delegate prompt is involved.

`--continue` preserves conversation identity when reopening an idle worker; it is not the child-result wake mechanism. Interrupted tasks are not silently restored or replayed. Regression coverage uses an offline RPC peer to exercise the delegate boundary; no paid model-backed child-completion run is claimed.


Existing commands remain available: `session create/list/compact`, `turn SESSION MESSAGE`, `wait/read/events/steer/interrupt/answer JOB`. `turn` remains direct session dispatch; lanes are the routine interface.

## Advanced lifecycle

Use `session show/open/close/archive/unarchive/configure/delete ID` and `session list --archived` for explicit session operations. `open` optionally prewarms. Archive preserves history but disables dispatch; close leaves a conversation available for resumption. Configure while settled; reopened workers use the changed options without erasing history. `lane assign` requires `--expected-revision`; `lane delete` removes only its registration.

Deletion requires `--confirm-id` matching the session ID, and is restricted to archived, stopped, unassigned sessions. Purge affects delegate-owned session/job/event/log data, retaining minimal lane-history identity. It never deletes project files, worktrees, branches, provider history, credentials, or unrelated state. Filesystem removal is not secure erasure and cannot remove prior backups or exports.

Lane labels are coordination metadata, not authorization: clients sharing a daemon token share control. Subagent commands (`agent list/read/steer/cancel JOB ...`) use a parent job and child ID; cancellation is not a hard OS kill. Descendant cleanup depends on OMP support; detached tools, external effects, or unrelated processes may outlive shutdown.

## Upgrading existing state

Finish or explicitly interrupt existing jobs, stop the old daemon, update the code, then start the daemon with the same state directory and port. The current state format is version 2; startup migrates version 1 and legacy `sessions.json`, preserving sessions, jobs, transcripts and options. The legacy metadata file is removed only after the new state is written. Do not run an older daemon against version 2 state. Existing sessions are not assigned to lanes automatically: create a lane and explicitly adopt a saved session with `lane assign` if needed.

Worker processes are runtime resources, not persisted conversations. After restart, workers start stopped and unfinished jobs remain interrupted/aborted; no prompts are replayed. Archived state and lane assignment/history persist. Failed cleanup keeps a worker quarantined rather than admitting a replacement writer. An interrupted purge is recovered on the next startup.

Do not run an older version against a migrated state directory: it does not understand the new metadata. There is no automatic idle timeout or transcript expiration. Grok decides when to close workers; deletion is a separate, explicit operation.

## Reuse your backend setup

OMP workers run the installed `omp` with its inherited environment and the target project's `--cwd`. OMP discovers global credentials, skills, extensions, custom tools and configured models, plus target-project configuration such as `.omp/config.yml` and instructions such as `AGENTS.md`. Codex workers use the operator's installed, authenticated Codex CLI and its own configuration and project instructions through the persistent headless app-server; they do not use `codex exec`. Project-local setup applies to that target project; it is not exported to unrelated projects. Provider credentials are neither copied into this repository nor returned to Grok. An explicitly permitted tool can still access files available to the OS user; the delegate is not a credential-isolation sandbox.

Session and lane creation accept `--backend omp|codex` (default `omp`) plus backend-specific worker options. `--config` and `--add-dir` paths must be absolute and already exist; relative paths are rejected. Options are saved with the session; use `session configure ID` while settled to change supported options, but backend selection is immutable. OMP profiles retain their native credentials/configuration behavior; Codex option limits are listed above.


RPC pins some OMP defaults differently from interactive mode, notably task isolation/execution, memory, advisor and async behavior. Explicit OMP global/project configuration and overlays remain authoritative. This delegate preserves discovery; it does not promise that an RPC worker behaves identically to the current interactive chat. Tools needing interactive MCP login must be authenticated separately before delegation.

Environment variables:

| Variable | Purpose |
| --- | --- |
| `OMP_DELEGATE_STATE` | Private state directory; default `~/.omp-delegate` |
| `OMP_DELEGATE_PORT` | Loopback HTTP port; default `4510` |
| `OMP_DELEGATE_OMP` | OMP executable path; default `omp` |
| `OMP_DELEGATE_CODEX` | Codex executable path; default `codex` |

`--state-dir` and `--port` precede the CLI subcommand and override their environment defaults. Daemon environment changes take effect after a restart.

## Approvals and questions

Approval mode is always yolo; the daemon enforces it on both backends.

Every worker is stored with `approval_mode: yolo`. OMP workers start with `--approval-mode yolo`; Codex workers get `never` approval plus `danger-full-access` (Codex's full-auto equivalent). `approval_mode` is still accepted on `session create`/`configure` and the CLI for compatibility, but any value is stored as `yolo`; a non-yolo request writes one line to the daemon's stderr log and sends no webhook. Approval mediation is not an OS sandbox. Never suppress an unresolved approval or grant permission on the coordinator's behalf.

A job with a pending request reports `status: waiting_input` and `pendingInput`. Conversational webhook delivery includes the full question request and `input_id`; Grok answers through the existing control path only after resolving the request under the owner's policy:

```sh
omp-delegate answer JOB_ID --json '{"id":"UI_REQUEST_ID","confirmed":false}'
omp-delegate answer JOB_ID --json '{"id":"UI_REQUEST_ID","answers":[{"id":"QUESTION_ID","selectedOptions":["EXACT OPTION"]}]}'
```

Use the exact request ID and values/options from `pendingInput`. OMP's own dialog timeout behavior still applies; for example, an `ask` timeout can select recommended/first options. Codex requests are answered through the same CLI workflow using native Codex replies.

## Grok Bot workflow

Load [`skills/bot-omp-delegate/SKILL.md`](skills/bot-omp-delegate/SKILL.md) into Grok Bot's skills. Give Grok the installed absolute `omp-delegate` path and target project path. For conversational webhooks, configure its subscriber with `mode: "conversation"` and pass that ID as `--coordinator-subscriber-id` when submitting a detached job. Submit once; Grok handles a question via `answer`, receives the full final response, and continues in the same session. Use `read`/`wait` to inspect or recover a job, not as a second foreground conversational wake path.

The Grok Mac app does not imply local shell access. Run the delegate in an executor that can reach the local daemon, selected CLI installation, and worktree.


## Lifecycle and safety

- One active delegate job per resolved working directory, across all delegate sessions. Use separate clones/worktrees for parallel jobs. This cannot detect writers outside the delegate or all overlapping parent/child directories.
- Sessions and job snapshots are private durable JSON files. OMP transcripts live in each delegate session's `sessionDir`; Codex thread identity is stored privately there while native Codex history remains Codex-managed.
- OMP completes only after session settlement, including resumed parent output. Codex completes on its owning `turn/completed`; this is not an OMP-style guarantee that detached child work has settled.
- After a crash, unfinished jobs are marked `interrupted` on restart. A graceful shutdown may let OMP report `aborted` first. Prompts are never silently replayed. Read the transcript and inspect actual work before resuming. Forced termination may leave OS descendants running; interruption and daemon shutdown are not a security kill switch.
- The daemon listens only on `127.0.0.1`, authenticates every route with a private bearer token, and rejects browser Origin headers. Never port-forward it publicly. The CLI reads the token internally; do not print or share it. Logs/transcripts may contain project data and prompts.
- CLI-to-daemon requests bypass HTTP proxy settings, keeping the local bearer token and task requests out of configured proxies. The OMP worker still inherits its environment, including proxy settings needed by its providers or tools.
- `interrupt` uses OMP's abort-and-restore-queue operation to prevent queued instructions from starting a fresh turn. Already scheduled external effects may still land. Verify the working directory before the next job.

## Publishing safely

Publish the source project, not its runtime state or a copy of your OMP home directory. The default state directory (`~/.omp-delegate`) is outside this repository. Keep any custom `--state-dir` outside the repository too: it contains the daemon token, project paths, prompts, tool output and session transcripts.

The `.gitignore` excludes common local environment files, credential/key files, logs, OMP-local state and Python caches. Ignore rules do not remove files already committed and do not prevent force-adding files. Review the staged files and scan the Git history before publishing; recheck after adding configuration, fixtures or logs.

Provider credentials stay with each operator's OMP or Codex installation. That is not a guarantee that runtime output is secret-free: prompts, tool output and extensions can expose sensitive data if permitted. Review any transcript or diagnostic attachment before sharing it.


## Development

```sh
python3 -m unittest discover -s tests -v
```

