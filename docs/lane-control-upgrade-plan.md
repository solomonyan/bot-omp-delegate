# Full lane control upgrade plan

Status: approved design, subsequently implemented locally on the owner's explicit implementation instruction, 2026-10-31. The sections below retain the approved interface and acceptance specification. Verification is recorded in CHANGELOG.md. No dashboard prototype, commit or push is included in this implementation.

## Principle

Grok is the controller of each lane's complete coding lifecycle. The daemon exposes deterministic control and observable outcomes; it does not decide when a conversation is finished. The human dashboard remains strictly read-only. Full control does not mean bypassing approval policies, expanding OS permissions, deleting worktrees, or claiming to observe Grok's internal orchestration.

Use one daemon for multiple lanes, with three separately verified worktrees initially and no hard-coded lane-count limit.

## Approved simplicity constraint

Minimize Grok's coordination and context overhead, not merely the number of internal states. Grok uses lane-level actions for routine work and one returned job handle for job-specific controls. The daemon owns session resolution, worker generations, lifecycle reservations and transition checks. No model calls are required merely to update local lifecycle bookkeeping.

The two new everyday concepts are **close this lane's worker without losing its conversation** and **start a fresh conversation in this lane without deleting old history**. Reopening is automatic on the next task. Do not require a separate open step, manual session reassignment or worker-generation bookkeeping for ordinary work.

Keep the normal Grok skill short. Advanced archive/delete/configuration/subagent procedures belong in an on-demand reference. Host metrics and dashboard event streams do not enter Grok's context automatically. Use one foreground waiter per job; no repeated model-driven progress checks. Runtime and token overhead have not been benchmarked and must not be advertised as measured.

## Current gaps

Observed CLI session commands are create, list and compact. The daemon has session records and per-session OMP workers, but no lane records, individual worker close, archive, session deletion or idle timeout. Workers normally remain resident after a job settles. Existing control supports job steering, interrupt, question/approval answers and event inspection; it does not expose OMP subagent control. Worker status, ownership transitions and conversation lifecycle need explicit representation rather than inference from activeJob alone.

## State and terminology

- **Lane:** persistent named work slot, associated with an existing canonical worktree path, current session, assignment revision and historical session assignments. A controller label supports coordination but is not an authorization boundary: current clients share a control token. No claim of isolated per-bot permissions.
- **Session:** persistent OMP conversation. Durable state is available or archived; retain the transcript, options, last activity and associated jobs. Closing its worker does not close or delete the conversation.
- **Worker:** live subprocess, independently starting, idle, busy, closing, stopped or failed. Report process identity/generation and last error. A stopped worker is not a lost conversation.
- **Job:** one submitted task with admitted/running/waiting/settled outcome. Keep existing published job outcomes where applicable; add explicit startup/closing information without turning a yield into completion.
- **Subagent:** child activity belonging to a particular worker generation and job, not a globally reusable name.

Task completion preserves conversation and lane assignment. New conversation creation is an explicit controller action. Saved conversation history persists until an explicit deletion; there is no automatic history purge.

## Approved command interface

The lane controls below are implemented locally. Keep the previously published session/job commands unless a documented contract change is necessary. Lane and session addressing share one underlying lifecycle implementation, not duplicate worker logic.

### Routine Grok interface

| Command | Proposed behavior |
| --- | --- |
| lane create --name NAME --cwd PATH | One-time setup against an existing worktree. Register the lane and a fresh conversation record without starting a worker or model task. |
| lane list / lane show ID | Return compact lane/worktree, conversation availability, worker/job state, pending questions, relevant identifiers and last error. Do not dump conversation history or raw events by default. |
| lane run ID MESSAGE | Reuse the lane's assigned available conversation; lazily start/resume its worker. Return a job handle immediately, then wait by default. Support the existing explicit detach/deadline behavior. Fail explicitly for busy or archived/unavailable conversations. |
| lane close ID | Close only the current idle, settled worker; retain conversation and lane assignment. Already stopped is a no-op; busy/startup/compaction/closing conflicts are explicit. |
| lane close ID --interrupt | Explicitly withdraw queued instructions, abort admitted work, wait for settlement and close that worker. Return an incomplete/failed shutdown rather than falsely claiming closure. |
| lane new ID | Require settled work; close the old idle worker, create a fresh conversation using the lane's current configuration and atomically change the assignment. Preserve previous conversation/history; do not archive or delete it implicitly. |
| wait / read / steer / answer / interrupt JOB | Reuse the existing job-handle controls and foreground waiter behavior. Grok does not need session IDs, assignment revisions or worker generations for these normal operations. Controls remain bound to the returned job, never whichever task happens to be current later. |

### Advanced controls: load instructions only when needed

Retain the full lifecycle capability without making it part of every task's instructions:

- Explicit session create/show/list/open/close/compact operations. Open is optional prewarming, not a normal prerequisite to run.
- Session archive/unarchive: stop settled workers, preserve history and reject dispatch while archived.
- Session configuration changes while settled: close an idle worker so reopening applies the new options without erasing the conversation. Broader approval authority still requires owner authorization.
- Explicit lane assignment of a pre-existing session and lane registration removal. Advanced assignment can use an expected revision; daemon-internal generation/revision checks remain mandatory for all transitions.
- Session deletion with explicit confirmation: only archived, stopped sessions not currently assigned to a lane. Purge delegate-owned transcript/job/event/log data; retain only minimal identity tombstones required by lane history. Never delete project files, branches, external artifacts, provider history, global OMP credentials or unrelated state. Filesystem removal is not secure erasure and does not remove prior exports/backups.
- Supported OMP subagent list/read/steer/cancel, using the returned parent job handle and child identity with internal generation validation. Cancellation is not an OS-level hard kill.
- Raw events and full conversation inspection on demand, not continual ingestion into Grok's coordination context.

A closed available conversation reopens lazily when its lane receives work. Archive disables dispatch; deletion removes explicitly authorized retained data. The lane-level interface hides bookkeeping, not errors or consequential actions.

## Controller workflow

1. Grok lists lanes and reuses a suitable lane/worktree, or performs one-time lane creation under the project's worktree conventions.
2. For related work, run a task in that lane. The daemon resolves/resumes the conversation; Grok retains the returned job handle and assigns one foreground waiter.
3. Use the existing job controls for inspection, steering, allowed answers and interruption. Load detailed history or subagent instructions only when relevant.
4. After settlement, keep the worker for an immediate follow-up or close the lane's worker to release resources. The conversation remains resumable on the next run.
5. For unrelated work, explicitly start a new conversation in the lane. The daemon performs safe closure and assignment; Grok does not manually coordinate session IDs or generations.
6. Archive, reconfigure or delete only when that advanced operation is actually needed. Deletion requires explicit retention/deletion authorization and is never routine task cleanup.

No automatic task queue, retry, prompt replay, reboot supervisor, Git worktree provisioning/deletion or true process suspension is included. Interruption followed by a new prompt is a new task, not transparent checkpoint/resume. An optional configurable idle-worker timeout may be added only as a separate agreed policy; default remains explicit Grok-controlled closure.

## Concurrency, recovery and scope of safety

- Serialize per-session lifecycle transitions and per-lane assignments. Reserve canonical worktree ownership through startup, compaction, active work and shutdown until the prior worker is confirmed stopped/settled as appropriate. Reject stale revisions and controls for old job/generation identities.
- Never wait for RPC responses while holding a lock needed by the worker event reader. Snapshot transitions under lock, perform subprocess/RPC work outside it, then commit only if generation/state still match.
- Closing lane 1 must not interrupt lane 2 or 3. Duplicate close/open calls must not create extra workers or corrupt history.
- Default close never interrupts work. Explicit interrupt-close uses abort-and-restore-queue; pending dialogs, queued instructions and active child work must receive defined outcomes.
- Use bounded graceful process shutdown with explicit failure state. Verify descendant cleanup supported by OMP; external effects, detached tools and unrelated processes cannot be promised to stop. Do not replace a potentially live writer with a second worker while cleanup is unresolved.
- Preserve existing restart behavior: unfinished jobs become interrupted/aborted, not automatically replayed. Clear stale runtime ownership without erasing archived state or conversation identities. Deletion must be recoverable across partial metadata/file cleanup without exposing tokens or deleting outside the intended state root.
- Preserve the localhost bind, proxy-disabled CLI transport, private state permissions, installed OMP configuration discovery and no credential-copying policy.

## Implementation sequence

1. **State model and migration:** add a versioned additive state migration, worker generation/state, explicit lifecycle reservations and lane records. Existing sessions/jobs/transcripts retain identities and remain unassigned until Grok explicitly adopts them into lanes. Drain running work before upgrade; if not drained, existing interrupted-job recovery applies.
2. **Minimal worker lifecycle:** implement individual settled-worker close, explicit interrupt-close and lazy reopen with observable outcomes. This fixes the resource-release gap without requiring Grok to learn an open-before-run workflow.
3. **Simple lane orchestration:** implement lane create/list/show/run/close/new. Put safe conversation replacement, session resolution and generation/revision handling inside the daemon. Preserve the existing job-handle controls and one-writer invariants.
4. **Advanced lifecycle and child controls:** expose archive/unarchive, configuration, explicit session assignment/removal, optional prewarming and supported subagent list/read/steer/cancel through the same lifecycle implementation. Document these separately from the short normal workflow.
5. **Retention and Grok cutover:** implement explicit purge; update the concise Grok skill and its on-demand reference, CLI help, README, changelog and affected tests/callers. Teach task completion versus worker closure versus fresh conversation, with archive/delete as advanced actions. Only publish after verification and explicit push authorization.

Use the current Python standard-library deployment and existing JSON persistence patterns unless concrete correctness constraints establish a need to change them. One lifecycle module owns behavior shared by CLI, HTTP handlers and future read-only dashboard snapshots. No UI or Grok-side telemetry implementation in this upgrade.

## Acceptance and verification

- Start three actual OMP workers against separate scratch worktrees and run three model-backed jobs concurrently; inspect their distinct session/job identities and completion.
- Close one settled lane worker and observe its PID exit while other lanes remain usable. Run another task using only the lane ID and prove lazy reopen preserves conversation memory.
- Default-close a busy worker and verify an explicit conflict with no interruption. Exercise explicit interrupt-close during startup, a pending question, active tools and child activity; verify outcomes, worker exit and other-lane continuity. Document external-process cancellation limits from observed results.
- Archive a session and verify new tasks are rejected; unarchive and resume the same conversation. Explicitly replace a lane's conversation and retain old history.
- Race assignment, open, dispatch, compaction, close and stale-job control. Assert no second writer, no command delivered to the successor job and no false closed result.
- Delete only a stopped archived unassigned scratch session; verify intended delegate data disappears while project files, other lanes, global OMP state and credentials remain untouched. Exercise interrupted cleanup recovery.
- Prove model/configuration changes take effect on reopen without erasing the transcript or broadening permissions implicitly.
- Migrate a copy of existing-format state and restart during a lifecycle transition; preserve histories/archives and do not replay prompts.
- CLI and HTTP results expose consistent lifecycle states and stable errors. Run the full regression suite, then actual CLI/OMP smoke journeys; tests alone are not proof of subprocess/resource behavior.
- Repeat the staged-file sensitive-data audit and clean-distribution startup/proxy regression before any authorized release.
- Exercise the complete routine through lane create/show/run/close/new and existing returned-job controls, without requiring Grok to supply a session ID, generation or assignment revision. Fresh-conversation replacement preserves old history; close followed by run resumes the original conversation.
- Verify default lane inspection is compact and does not return full transcript/event/host-metric dumps. Confirm advanced procedures are disclosed on demand rather than embedded in every task's skill instructions.

## Relationship to the dashboard plan

The dashboard stays read-only; Grok gains the control interface. These lifecycle events/states can later feed its orchestration view. Observing Grok's own orchestration remains a separate, unverified integration: expanding delegate control does not automatically expose Grok's internal plans, conversations or tool activity.
