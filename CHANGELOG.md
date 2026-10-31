# Changelog

## Unreleased

- Persistent lanes with compact create/list/show/run/close/new workflows. Closed workers lazily resume the same conversation; new conversations retain previous history.
- Advanced session open/close/archive/unarchive/configure/delete, revision-checked lane assignment/removal, and parent-job-bound subagent list/read/steer/cancel.
- Versioned legacy-state migration and recoverable scoped purge. Serialize lifecycle transitions, quarantine failed cleanup, and reject stale controls and cross-task child transcript access.
- Bounded OMP worker/process-group shutdown, including interrupt-close during startup, prewarming, pending questions and active work. Detached/external processes remain outside the guarantee.
- Concise routine Grok skill with advanced instructions loaded on demand; no frontend, new dependencies, automatic idle timeout or Grok-native telemetry integration.

Verification: 36 regression tests passed. Real OMP smoke on macOS exercised three simultaneous model-backed lanes; isolated worker close and conversation-preserving reopen; fresh conversations; pending-question and active-tool interruption; startup cancellation; configuration applied on reopen; archive/unarchive; scoped deletion; explicit assignment, prewarming and lane removal; actual subagent transcript reads, steering, cancellation and stale-parent rejection. Independent source-only correctness and deletion/security reviews found no remaining blockers after repairs. Linux and Grok cloud execution/notification behavior remain unverified.

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
