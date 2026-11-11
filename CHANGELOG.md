# Changelog

## Unreleased
- Add persistent Codex app-server sessions selectable at lane/session creation, with immutable backend choice and shared detached webhook/answer workflow; document Codex setup, option and approval mappings, history retention, and OMP-only controls.
- Codex verification on macOS with CLI 0.167.1: 86 targeted regression checks passed; real model-backed smokes retained conversation memory across worker close and daemon restart, delivered full owner-routed responses, rejected an OMP writer while Codex owned the worktree, mediated a native command approval with explicit denial, and exercised native steering/interruption while approval was pending. Offline native-peer coverage includes questions, compaction settlement, stale events, failed resumption and transport exit. Production daemon and external Grok receiver were not changed or exercised. Codex named profiles and direct OMP-style child controls are explicitly unsupported.
- Fix Codex interrupt-close racing with an already completed native turn: retain worktree ownership until its pending terminal event is recorded, then close normally instead of leaving the session in `closing`. A deterministic stdio-peer regression reproduced the stuck state before the fix; all 16 targeted backend/startup/control checks passed afterward. The pre-fix full suite passed 106 tests under the CI Python 3.11 version.

- Add a versioned `omp-dispatch` ticket wrapper that preserves session approval settings instead of reconfiguring permissions before every job. Keep machine-specific subscriber discovery and monitoring in private local adapters.
- Route conversational notifications (`run.done`, `run.error`, `run.needs_input`) only to the recorded coordinator subscriber; add conversation/monitor modes, full response/input payloads, stable event identity, and event-based receiver deduplication guidance. Operational events remain monitor-only.
- Grok's conversational workflow uses one detached job and its owner-routed webhook; questions use the existing answer path, and follow-up continues in the same session. OMP approval configuration is inherited unless an explicit mode is selected.
- Conversation verification: 53 targeted delegate tests and 13 receiver tests passed. Installed OMP 18.18.2 with a loopback deterministic model verified inherited auto policy with zero approval webhooks, caller question/answer continuation in the same session, full final replies, and preserved explicit `write` approval. A sender/receiver/loopback-bot retry smoke produced two HTTP attempts with one forwarded question. The idle production daemon and local receiver were restarted; existing Grok subscribers now exclude operational events, and the payment lane retains `write`. External Grok endpoint delivery was not exercised.
- Preserve native same-parent OMP result delivery through background-work settlement; cover the real Worker/Store boundary with an offline RPC peer, without an extra parent prompt or launch/config change.
- Best-effort outbound webhook notifications with private configuration, filters, HMAC signing, bounded retries, sanitized receipts and explicit connectivity testing.
- Versioned run completion/error/deadline/input/stall, optional lane-idle and daemon-start events, ticket labels, and atomic idle-only daemon stop/restart.
- Persistent lanes with compact create/list/show/run/close/new workflows. Closed workers lazily resume the same conversation; new conversations retain previous history.
- Advanced session open/close/archive/unarchive/configure/delete, revision-checked lane assignment/removal, and parent-job-bound subagent list/read/steer/cancel.
- Versioned legacy-state migration and recoverable scoped purge. Serialize lifecycle transitions, quarantine failed cleanup, and reject stale controls and cross-task child transcript access.
- Bounded OMP worker/process-group shutdown, including interrupt-close during startup, prewarming, pending questions and active work. Detached/external processes remain outside the guarantee.
- Concise routine Grok skill with advanced instructions loaded on demand; no frontend, new dependencies, automatic idle timeout or Grok-native telemetry integration.


Continuation/wake verification: `python3 -B -m unittest discover -s tests -v` passed all 83 tests. Separate CLI/HTTP/Worker smoke with an offline RPC peer retained the same parent session and worker process, sent one prompt, captured the resumed result and delivered one completion webhook. A loopback retry smoke against the existing receiver code with temporary state produced two HTTP attempts sharing one delivery UUID and one coordinator forward attempt. No paid model-backed engine run or live Grok endpoint was exercised; the production daemon/config/receiver/ledger were left untouched. Source activation requires a later authorized daemon restart.

Webhook verification: all 67 tests passed, including the existing 36 regressions. A real model-backed OMP smoke observed all seven event types, verified the label and target-worktree Git metadata, and exercised idle-only restart. An isolated loopback receiver verified authentication and HMAC and returned HTTP 204 for connectivity tests before and after restart. Coverage includes filters, stall suppression/rearming, subscriber reloads, invalid timer configuration, four-attempt retry isolation, queue saturation, redaction, redirects/proxies, trickled-header deadlines and bounded graceful shutdown. External coordinator delivery and Linux remain unverified.

Earlier lane-lifecycle verification: 36 regression tests passed. Real OMP smoke on macOS exercised three simultaneous model-backed lanes; isolated worker close and conversation-preserving reopen; fresh conversations; pending-question and active-tool interruption; startup cancellation; configuration applied on reopen; archive/unarchive; scoped deletion; explicit assignment, prewarming and lane removal; actual subagent transcript reads, steering, cancellation and stale-parent rejection. Independent source-only correctness and deletion/security reviews found no remaining blockers after repairs. Linux and Grok cloud execution/notification behavior remain unverified.

Clean-distribution verification also passed all 36 tests, daemon startup with an unreachable configured HTTP proxy, and real prewarm-startup cancellation. Sensitive-data patterns and high-entropy Python literals were checked across all 18 intended source/documentation files with no findings; no runtime state or credentials were included.

## 0.1.0 — 2026-10-31

- Standalone authenticated loopback daemon, named persistent OMP workers, foreground/deferred jobs and long-poll waiting.
- Steering, abort-and-restore-queue interruption, question/approval responses, compaction and paged persisted events.
- Inherit installed OMP environment, global skills/extensions and target-project setup; default to always-ask approvals without copying credentials.
- Persist job outcomes and protect delegate-owned working directories from concurrent jobs; recover without replaying interrupted work.
- Grok Bot delegation skill with a foreground waiter workflow and explicit approval escalation.
- Document existing-project configuration inheritance, session continuity and same-machine deployment flow with generic paths.
- Exclude common environment, credential, key and log files from publication; document separation of source and private runtime state.
- Bypass environment-configured HTTP proxies for local daemon requests, with a regression covering token and prompt disclosure.

Verification on macOS with Python 3.15.0 and OMP 18.14.1: real model-backed dispatch and completion; conversation memory after daemon restart; ask/answer; explicit approval of a harmless bash command and its output; same-directory conflict; unauthenticated HTTP rejection; interruption; steering; a long poll exceeding ten seconds; waiter deadline preserving the job; graceful daemon restart with a pending job. Unit coverage also exercises chunk framing, worker EOF/failure and unsettled-session recovery. Grok's cloud sleep/notification behavior and Linux deployment were not exercised.
