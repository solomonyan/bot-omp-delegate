# Changelog

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
