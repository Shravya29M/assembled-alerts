Assembled take-home \| Revised implementation blueprint \| 6 November 2026

Implementation specification, not a claim of completed code. Incorporates the assignment requirements, the original plan, and corrections verified against the supplied 96-event JSONL.

# 1. Product goal and scope

Build a small, configurable intraday notification system for contact-center team leads. A lead chooses queue/agent scope, thresholds and recipient, replays operational events, and sees actionable notifications. Optimize for correctness, understandable behavior, low noise, and a runnable demo—not breadth.

- P0: four editable, enable/disable rule templates: backlog, SLA risk/breach, adherence duration, long-call duration.

- P0: seeded, explicitly illustrative team-lead ownership; visible notifications and active incidents; one active incident per rule × subject.

- P0: deterministic file-order replay with Step, Run All and Reset; virtual duration timers, stale-event protection, event-ID deduplication, and automated tests.

- Deferred: volume-versus-forecast rule, agent self-service, Run Until, Slack/email, authentication, production deployment, distributed workers and general-purpose rule DSL.

Audience decision: team leads only. The architecture may support other recipients later, but do not present agent self-notifications as implemented.

# 2. Assignment alignment

| Criterion        | How this build demonstrates it                                                                                                        |
|------------------|---------------------------------------------------------------------------------------------------------------------------------------|
| Product thinking | Focused audience, understandable configuration, explicit alert noise policy and deferred features.                                    |
| Systems design   | Typed events, state/rule/incident separation, replay semantics, idempotency; explain production partitioning without implementing it. |
| Code quality     | Small modules, pure rule evaluation, validated inputs, deterministic boundary and dataset tests.                                      |
| AI judgment      | Disclose AI use; independently verify suggested checkpoints and duration inconsistencies against source records.                      |

# 3. Dataset facts and routing assumptions

96 events: 24 queue_snapshot, 31 agent_state_change and 41 adherence_check; 3 queues (billing, tier_2, vip) and 8 agents. Event times span approximately 09:00–10:30 UTC on 2026-05-26. File order is treated as ingestion order, but the source does not provide actual ingestion timestamps.

- Seed demo ownership: lead_billing → billing, vip, a_31, a_19, a_07, a_05; lead_support → tier_2, a_23, a_42, a_11, a_88. These assignments are illustrative, not source-derived.

- a_05 has queue_ids=null at 10:01:40 and \[\] in a subsequent record. Accept nullable queue lists; agent-level routing uses seeded agent ownership, never inferred queue membership.

- Billing snapshot at 10:00 has volume_forecast_next_15m=null. Keep other queue-rule evaluations; mark forecast missing rather than converting to zero.

- a_31 exits a 09:30–10:25 call with reported previous_state_duration_sec=3900, while timestamp-derived duration is 3300 seconds. Trust accepted transitions for timer behavior; record a lightweight mismatch diagnostic.

- A repeated event ID evt_01HXYZ050 (file line 95; ts 09:36:30 versus the original 09:36:00, so the payloads differ) and a final stale VIP 09:49 snapshot after a 10:30 snapshot require deduplication and freshness protection. The stale VIP snapshot (1 waiting, 35/60 s) would not alert even if accepted, so freshness is also tested with a synthetic stale event that would otherwise alert.

- a_23's adherence check at 10:15:30 reports actual_state=in_meeting, but the state-change stream has a_23 on_call since 09:45 with no exit. Accepted agent_state_change events stay authoritative for call state. Record a conflicting-activity diagnostic. Do not end or alter the call episode.

# 4. Rule configuration and exact semantics

| Rule      | Trigger / escalation                                                                                                                               | Clear / timing                                                                                                               |
|-----------|----------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| Backlog   | Warning tickets_waiting \>15; critical \>20.                                                                                                       | At 20 downgrade critical→warning; resolve at ≤15. No backlog hysteresis.                                                     |
| SLA       | Warning longest_wait_sec ≥0.80×sla_target_sec; critical \> target.                                                                                 | Resolve at ≤0.75×target. Critical can open directly.                                                                         |
| Adherence | Only in_violation=true confirms violation; alert after ≥10 min from valid violation_started_at; if null, measure from first confirmed observation. | Resolve on newer accepted in_violation=false check; do not infer from meeting state alone.                                   |
| Long call | Accepted on_call transition starts episode; alert at ≥45 min while still on_call.                                                                  | Accepted state exit cancels timer and resolves active alert; previous_state_duration_sec is diagnostic, not timer authority. |

Rule UI: type, scope (queue or agent/all owned subjects), threshold/duration, recipient, enabled toggle and a plain-English preview. Validate inputs and restrict recipient to the seeded demo map. Rule edits apply on the next replay evaluation; for timer-rule edits invalidate existing timers and re-evaluate current accepted state at the simulated clock. Clearly label this policy in the UI.

# 5. Incident lifecycle and noise policy

One active incident per (rule_id, subject_id). Immediate rules may open at Warning or Critical; duration rules begin Pending, then notify when due. Notify on opening, Warning→Critical escalation and resolution. Do not notify repeatedly at unchanged severity. Critical→Warning updates incident silently. A later breach after resolution creates a new incident. Disabling a rule closes its incident as disabled, not recovered, and cancels pending timers.

Billing SLA recovery at 10:15, new warning at 10:16 and recovery at 10:30 are distinct transitions under these rules. Do not invent a cooldown or two-snapshot requirement. At replay end, leave unresolved incidents visibly ACTIVE; do not manufacture resolutions.

# 6. Event engine, clock and timers

- Process source records in original file order; maintain simulated clock C=max(C,event.ts). Do not rewind when a late event arrives.

- Validate event shapes, including optional/null queue IDs and forecast. Deduplicate on event_id before state mutation; record duplicates, and optionally a simple differing-payload diagnostic without a separate conflict subsystem.

- Track latest accepted timestamp per subject and event family. Stale observations do not overwrite state or create alerts.

- Use a deadline min-heap keyed by (deadline, sequence, rule, subject, episode_version). Exits, disable and edits invalidate old timer versions.

- Timer firing order: a live timer with deadline D fires only when the next accepted event has ts > D. It fires before that event is applied, in heap order. It can also fire at end of input if D ≤ horizon. So every accepted event with ts = D is applied first. That includes a state-ending event, wherever it sits among same-timestamp events in file order, so the ending event invalidates its timer before the timer can fire. Duplicate, stale and invalid events never advance the clock or trigger timer firing. Test this explicitly.

- Track effective_at separately from emitted_at. Immediate rules: both equal the triggering event's ts. Timer rules: effective_at = deadline D. emitted_at = the simulated clock at the firing step, which is the ts of the first accepted event with ts > D, or the horizon at end of input. So emitted_at is never earlier than the evidence that made the alert certain. Never claim a notification was delivered in the past.

- The observation horizon is the simulated clock at end of input (10:30 for this dataset), and it is inclusive. At end of input, fire live timers with deadline ≤ horizon. Never fire a timer with deadline > horizon. Run All followed by Reset and Run All must reproduce the same ordered results.

## Pure evaluation boundary

Separate ingestion/state mutation from rule evaluation. Conceptually evaluate(rule, current_state, clock) returns a condition/severity decision without writing to storage. The orchestrator owns timers, incident transitions, routing and persistence. Avoid claiming the entire event engine is pure or stateless.

# 7. Verified dataset checkpoints (not a complete oracle)

| Effective time UTC | Expected condition                                                                                      |
|--------------------|---------------------------------------------------------------------------------------------------------|
| 09:30              | Billing backlog warning (18); Billing SLA directly critical (130/120).                                  |
| 09:36              | Billing backlog critical (22); a_19 violation confirmed (start 09:35).                                  |
| 09:40              | Billing backlog downgrade at 20 (silent).                                                               |
| 09:45              | Tier_2 SLA warning (250/300); a_19 adherence duration reaches 10 min.                                   |
| 09:55              | a_11 long call reaches 45 min (09:10–10:20 episode). ADDED.                                             |
| 10:00              | Billing backlog resolves (14); tier_2 SLA critical (320/300); billing forecast is null.                 |
| 10:10              | a_07 second call reaches 45 min; a_88 adherence reaches 10 min.                                         |
| 10:10:30           | a_19 adherence resolves on the in_violation=false check, not at the 10:10:00 state change. ADDED.       |
| 10:15              | a_31 second call reaches 45 min (09:30–10:25). ADDED. Billing SLA resolves at 75%; tier_2 SLA resolves. |
| 10:16              | Billing SLA reopens warning (110/120).                                                                  |
| 10:20              | a_11 long call resolves on exit to available. ADDED.                                                    |
| 10:25              | a_31 long call resolves on exit; 3900 vs 3300 duration-mismatch diagnostic. ADDED.                      |
| 10:25:30           | a_23 first-observed adherence violation reaches 10 min.                                                 |
| 10:30              | Billing SLA resolves again; a_23 call reaches 45 min under authoritative state-change policy.           |

These are effective condition times, not necessarily actual emitted_at values under file-order replay. Generate the complete notification oracle by executing the finished engine, then independently inspect and freeze it as a test fixture. Include negative expectations (e.g., a_07 first short call, stale VIP snapshot, invalidated timers).

## 7.1 Provisional notification oracle (hand replay, file order)

Provisional: this was derived by hand. It is not frozen until the finished engine reproduces it under deterministic file-order replay and every difference is inspected. Rows are in emitted order. Ties at the same firing step follow heap order (deadline, sequence), so timers fire before the event that triggered the step is applied. Within one event, rules are evaluated in rule_id order.

| #  | emitted_at | effective_at | Rule / subject    | Transition                             | Lead         |
|----|------------|--------------|-------------------|----------------------------------------|--------------|
| 1  | 09:30      | 09:30        | Backlog / billing | open warning (18)                      | lead_billing |
| 2  | 09:30      | 09:30        | SLA / billing     | open critical (130/120)                | lead_billing |
| 3  | 09:36      | 09:36        | Backlog / billing | escalate → critical (22)               | lead_billing |
| 4  | 09:45      | 09:45        | SLA / tier_2      | open warning (250/300)                 | lead_support |
| 5  | 09:48      | 09:45        | Adherence / a_19  | open (start 09:35)                     | lead_billing |
| 6  | 10:00      | 09:55        | Long call / a_11  | open (since 09:10)                     | lead_support |
| 7  | 10:00      | 10:00        | Backlog / billing | resolve (14)                           | lead_billing |
| 8  | 10:00      | 10:00        | SLA / tier_2      | escalate → critical (320/300)          | lead_support |
| 9  | 10:10:30   | 10:10        | Long call / a_07  | open (since 09:25)                     | lead_billing |
| 10 | 10:10:30   | 10:10        | Adherence / a_88  | open (start 10:00)                     | lead_support |
| 11 | 10:10:30   | 10:10:30     | Adherence / a_19  | resolve                                | lead_billing |
| 12 | 10:15      | 10:15        | SLA / billing     | resolve (90 = 0.75×120)                | lead_billing |
| 13 | 10:15      | 10:15        | SLA / tier_2      | resolve (180/300)                      | lead_support |
| 14 | 10:15:30   | 10:15        | Long call / a_31  | open (since 09:30)                     | lead_billing |
| 15 | 10:16      | 10:16        | SLA / billing     | open warning, new incident (110/120)   | lead_billing |
| 16 | 10:20      | 10:20        | Long call / a_11  | resolve                                | lead_support |
| 17 | 10:25      | 10:25        | Long call / a_31  | resolve                                | lead_billing |
| 18 | 10:30      | 10:25:30     | Adherence / a_23  | open (null start; first seen 10:15:30) | lead_support |
| 19 | 10:30      | 10:30        | SLA / billing     | resolve (30)                           | lead_billing |
| 20 | 10:30      | 10:30        | Long call / a_23  | open at end of input (since 09:45)     | lead_support |

Silent: 09:40 billing backlog critical → warning (20).

ACTIVE at end of replay (4): a_07 long call, a_23 long call, a_23 adherence, a_88 adherence.

Negative expectations: a_07 first call (09:05:30–09:18) and all other short calls do not fire. The a_42 call from 09:48 (deadline 10:33 > horizon) does not fire. a_19 adherence does not resolve at the 10:10:00 state change. Duplicate line 95 and stale line 96 produce no state change or notification. Billing backlog 19 and 17 repeat no notification. Null billing forecast at 10:00 does not block backlog or SLA evaluation.

# 8. Minimal implementation and persistence

Python 3.12+, FastAPI, Pydantic, SQLite, pytest, stdlib heapq; a single static/server-rendered HTML page with small vanilla JavaScript fetch calls. No React, broker, WebSockets or live delivery integration.

| Component                 | Responsibility                                                                |
|---------------------------|-------------------------------------------------------------------------------|
| ingest.py / models.py     | Typed parsing, validation, event-ID dedupe, freshness.                        |
| state.py / clock.py       | Latest accepted operational state, replay clock, timer episodes.              |
| rules.py                  | Pure threshold/severity evaluation.                                           |
| incidents.py / routing.py | Lifecycle transitions, seeded ownership, visible notification decisions.      |
| storage.py                | SQLite transaction around processed marker, state, incident and notification. |
| api.py + static UI        | Rules, replay controls, incident list, notification history, diagnostics.     |

SQLite tables: rules, operational_state, incidents, notifications, processed_events; ownership can be a JSON seed. Store active incident uniqueness on (rule_id, subject_id), and preserve immutable notification history. Local replay is serialized; do not imply concurrent workers are supported.

Endpoints: GET/POST /rules, PATCH /rules/{id}, GET /incidents, GET /notifications, POST /replay/step, /replay/run-all, /replay/reset, GET /replay/status. UI shows simulated clock, file position, counts, rule previews, active incidents and notifications.

# 9. Acceptance test plan

- Dataset replay: consume all 96 records once, deduplicate the repeated event ID, reject the stale final VIP snapshot, match §7.1 emitted order with effective_at/emitted_at, end with exactly four ACTIVE incidents, and reproduce identical output after reset.

- Long calls: a_11 09:55 (resolves 10:20); a_31 10:15 (resolves 10:25); a_07 second call 10:10; a_23 10:30. a_07 first call and a_42 second call (deadline 10:33) must not fire.

- Adherence: a_19 (opens 09:45, resolves 10:10:30, not 10:10:00), a_88, null-start a_23; only adherence checks establish or clear violations.

- SLA/backlog: warning/critical/recovery boundaries, silent downgrade, Billing 10:15→10:16 reopen.

- Null and inconsistency: a_05 null/empty queue IDs, billing missing forecast, a_31 duration mismatch, a_23 conflicting-activity diagnostic.

- Timer boundaries: exit at the exact deadline wins regardless of position among same-timestamp events; timer due exactly at the horizon fires; stale/disabled/edited timer does not fire; no timer beyond the horizon; synthetic stale event that would otherwise alert is rejected.

- Routing/noise: correct lead by subject, no repeated same-severity alerts, unknown ownership diagnosed not guessed.

- Rule edit and replay: edited thresholds have documented application timing; Run All/Reset behavior deterministic.

# 10. Build sequence and scope guardrails

| Phase                         | Deliverable                                                              |
|-------------------------------|--------------------------------------------------------------------------|
| 1\. Validate (1–2h)           | Freeze schema, semantics and independently checked expected checkpoints. |
| 2\. Engine (3–4h)             | Parsing, dedupe, freshness, state and pure rule decisions.               |
| 3\. Incidents + timers (3–4h) | Duration scheduling, cancellation, routing and persistence.              |
| 4\. API + UI (2–3h)           | Four editable templates; Step, Run All, Reset; visible notifications.    |
| 5\. Tests + README (3–4h)     | Dataset oracle, boundary tests, one-command run, demo and AI disclosure. |

Target: approximately 12–17 focused hours, a planning estimate only. If time is tight, cut visual polish and diagnostic embellishments before correctness, configurability or timer tests. Avoid introducing a fifth rule without validating its semantics.

# 11. README, demo and scaling discussion

README: problem/audience, one-command setup, example configuration, ownership assumptions, trigger and recovery semantics, event ordering, timer policy, missing data, sample verified outputs, limitations, tests, and accurate disclosure of AI tools used and how their output was verified.

Suggested review: 3 min problem/scope; 4 min rules UX; 7 min live replay (backlog escalation, SLA, long calls a_11/a_31, diagnostics); 5 min architecture and tests; 3 min tradeoffs/scale; remaining time for questions.

Production evolution (discussion only): tenant+subject keyed state and indexed rule lookup; partition event streams by entity with ordering guarantees; durable broker, PostgreSQL, persisted timers, transactional outbox, idempotent delivery workers and observability. Pure evaluation facilitates testing and scaling but does not itself guarantee scalability.

Forecast variance extension: volume_last_15m and volume_forecast_next_15m refer to different windows; do not compare directly as a factual forecast miss. A future rule needs forecast snapshots aligned to the measured 15-minute window and a documented missing-data policy.

Source basis: supplied Assembled assignment, original implementation blueprint and events.jsonl. Proposed policies are design decisions, not additional assignment mandates.
