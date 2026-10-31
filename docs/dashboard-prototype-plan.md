# Dashboard prototype plan

Status: planning brief approved 2026-10-31; scope subsequently expanded by the owner to all connected Grok bots and hybrid evidence with explicit labels. Approval covers planning only, not prototype construction, production implementation, or publication of new changes.

## Purpose and operating context

A local, authenticated, strictly read-only browser dashboard for supervising the complete orchestration of all connected Grok bots, not only their delegated OMP coding lanes. Attention-first: identify which coordinator, task or worker needs attention; understand objectives, delegation, dependencies, activity and returned results; distinguish workload activity from Mac resource pressure. Initial user is the operator on the same Mac. English interface; conversation and tool output retain their original language. Coverage includes only connected/instrumented sources, not automatic access to every Grok account or hidden model reasoning.

## Confirmed decisions

- Top-level bot roster covering every connected Grok coordinator, with its objectives, conversations, tasks, Grok subagents, OMP sessions, browser/tool work and external jobs where evidence is available.
- Support hybrid evidence: distinguish observed runtime/tool events, structured reports supplied by Grok, and unknown/unavailable activity. Never render a reported action as independently observed or infer a successful handoff from narrative text alone.
- Show orchestration relationships and chronological history: who assigned work to whom, dependencies, handoffs, waiting states, returned results and subsequent reported/observed actions. Relationships require explicit correlation; missing links remain unknown.
- OMP coding remains one branch of the full orchestration. Start with three persistent named coding-lane slots; three is not a hard limit. Grok owns conversation assignments and changes. Dashboard observes only.
- Distinguish coordinator, objective, task, worker/subagent, tool/external job and dependencies; within the OMP branch distinguish lane (work slot), session (coding conversation) and coding task (one request).
- Finishing a task does not close the conversation. Current and previous lane conversations remain distinct and browsable.
- Show available streaming conversations and expandable tool calls/results with coordinator → task → worker/subagent navigation; OMP drill-down includes session → task → subagent. Mark unavailable transcripts explicitly. Long tool output starts collapsed.
- Show machine CPU utilization/load, memory pressure, swap and available disk space; show worker/process counts and attributable CPU/memory where measurable. Keep machine totals separate from worker attribution. No invented health score. Temperature, fan, GPU and power metrics are not promised.
- Machine metric history is a rolling one-hour in-memory window, reset on daemon restart. Existing persisted conversation history loads on demand; this does not change conversation retention.
- Dashboard-only alerts for pending questions/approvals, failures, disconnections and measured resource pressure. No desktop notifications, sounds or automatic interventions.
- Silence is a last-event timestamp, not a stuck verdict. Unavailable/stale metrics are not zero or healthy.
- No frontend build chain: plain HTML/CSS/JavaScript prototype, preserving the prospective Python-served deployment model.

## Prototype question and alternatives

Which information structure best supports scanning multiple Grok coordinators, following end-to-end delegation and dependencies, distinguishing observed/reported/unknown activity, and recognizing computer pressure without losing conversation context?

Three alternatives at the same review URL, using identical simulated workload, scenarios and scenario controls:

1. **Overview + inspector:** connected-bot attention overview with selected objective, orchestration and conversation detail alongside; OMP lanes nested beneath their coordinator.
2. **Parallel coordinators:** aligned coordinator workstreams with delegation summaries, linked dependencies and a larger reading area for selected activity; coding lanes are nested rather than the whole dashboard.
3. **Orchestration graph + timeline:** coordinator/task/worker/tool relationships and dependencies with chronological evidence and conversation alongside; retain hierarchical navigation where a graph is unnecessary.

Machine pressure and actionable alerts remain visible in each alternative. These are structural candidates, not an approved palette, typography or visual identity. Establish visual direction before prototype code is written.

## Prototype interactions

- Switch connected Grok bots and inspect their current objectives, conversations and task history.
- Trace assignments, dependencies, handoffs and results through Grok subagents, OMP workers and other connected tools/jobs.
- Drill into OMP coding lanes and browse current/previous coding conversations and nested subagents.
- Inspect evidence origin and freshness; distinguish runtime observations from structured Grok reports and unknown gaps.
- Follow streaming messages and expand tool calls/results.
- Leave live-follow to inspect history and explicitly return to live.
- Inspect pending questions/approvals without answering them.
- Inspect one-hour host trends and worker attribution.
- Switch layout alternatives and exercise shared simulated scenarios.

All controls affect simulated viewing state only. No dispatch, steering, approval/denial, interruption, session management or machine-control actions.

## Required scenarios

- Multiple Grok coordinators working on separate objectives, including an objective that delegates to both an OMP coding worker and a non-coding worker/tool.
- Dependent work waiting for another result; a worker result returns and the coordinator proceeds.
- Native evidence missing for a reported delegation: show the report and coverage gap without inventing the child activity.
- Grok report and observed worker status disagree: preserve source/time labels and expose the mismatch rather than silently choosing a reassuring state.
- Three concurrently working lanes.
- Parent waiting while child agents continue.
- Question/approval waiting for Grok.
- Quiet agent with last-event timestamp.
- CPU/memory pressure and low disk space.
- Job failure and worker exit.
- Connection loss, stale data, reconnection and history replay.
- Task completion with reusable conversation.
- Grok assigning a new conversation to a persistent lane, preserving previous history access.
- Unavailable metrics labelled unknown.

Clearly label all sample conversations, events and resource readings as simulated. No real agent dispatch, production daemon integration, real collectors or real workload changes in this prototype. Simulation is not evidence that real instrumentation works.

## Delivery and review

- Clearly named throwaway prototype, outside production routes and main-branch implementation; no production/configuration/dependency/schema changes merely to host the prototype.
- One local review URL with three switchable layouts and shared scenario controls.
- Desktop-first; keyboard-operable, readable output and reduced-motion support.
- Review whether the operator can identify the coordinator and task needing attention, trace delegation and dependencies, inspect the relevant agent/tool, distinguish observed/reported/unknown activity, recognize stale information and machine pressure, and distinguish coding-task completion from conversation replacement.
- Inspect the rendered alternatives and interactive scenarios before handing over the prototype. Capture the selected direction and rationale before any production implementation.

## Production prerequisites, not prototype scope

The current delegate has OMP session records, job events and question forwarding, but no Grok-side orchestration event source, explicit lane model, browser interface or host-health collector. It does not currently enable OMP's subagent subscription. It cannot observe Grok's own planning, subagents, browser/tool activity or external jobs merely because Grok calls the delegate.

A production dashboard requires an inventory and feasibility check of the Grok runtime's locally available event/export/hook interfaces, plus a structured reporting path where native observations are unavailable. Do not promise a Grok event interface before establishing it. Correlate coordinators, tasks, workers and external jobs across sources; retain evidence origin and timestamps, show coverage gaps, and keep unknown activity unknown. Grok's reported plans/decisions are not access to hidden reasoning.

OMP integration additionally requires explicit lane assignment and subagent subscriptions. Shared dashboard prerequisites include browser authentication with read-only authority separate from the control token, continuous event streaming/replay/reconnection, safe rendering of sensitive/untrusted tool output, real metric collectors and verified worker-process attribution. Resource-warning thresholds and collection feasibility must be validated against real instrumentation rather than inferred from sample data. Closing a viewer must not stop workers.

The separate [full lane control upgrade plan](lane-control-upgrade-plan.md) defines proposed Grok-owned lane/conversation/worker lifecycle controls. Those controls do not belong in this read-only dashboard and do not establish Grok-side monitoring access.

This plan does not authorize an automatic commit or push. No prototype has been built as part of the planning step.
