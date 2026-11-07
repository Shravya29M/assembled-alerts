# Design Decisions

Each entry: decision — one-line reasoning.

1. **Hand-replayed oracle is provisional.** The 20-notification list in PLAN §7.1 is frozen only after the engine reproduces it and differences are inspected. — A hand replay can contain arithmetic or ordering mistakes, so neither it nor the engine is trusted alone.
2. **Observation horizon is inclusive (≤ 10:30).** — a_23's long-call deadline is exactly 10:30, and the last observation covers that instant.
3. **A timer with deadline D fires only once an accepted event with ts > D arrives, or at end of input.** — File-order replay cannot know all events at D have been seen until a later timestamp appears. This guarantees a same-timestamp ending event always cancels its timer, whatever its position in the file.
4. **emitted_at for timers is the clock at the firing step; effective_at is the deadline.** — This makes the replay lag visible (for example, a_11 is effective 09:55 and emitted 10:00) instead of claiming a notification went out before the engine could know it.
5. **Duplicate, stale and invalid events never advance the clock or trigger timers.** — Rejected input must have no side effects, or dedupe and freshness would not really be protections.
6. **a_23 conflicting activity is a diagnostic, not a state override.** — agent_state_change is the authoritative call-state source; adherence's actual_state is secondary evidence.
7. **Freshness is also tested with a synthetic stale event that would alert.** — The dataset's stale VIP snapshot would not alert even if accepted, so it cannot prove freshness protection works.

## Phase 1 — schema and parsing

8. **Python 3.12 via a uv-managed interpreter in `.venv`.** — CLAUDE.md requires 3.12; the system `python3` is 3.14, and uv already has CPython 3.12.14 installed.
9. **Parsing and validation live in Phase 1; dedupe, freshness, the simulated clock and state live in Phase 2.** — The schema must be checked against real data before the engine is built on it.
10. **Agent states are a fixed set: available, on_call, on_break, in_meeting.** — Verified against every previous_state, new_state, scheduled_state and actual_state value in the dataset; a fixed set catches typos. A production system might map unknown states to "other" so a new state label does not drop real transitions.
11. **Unknown extra fields are rejected.** — The dataset is fixed, so an unexpected field means we misunderstood the data. A production system would usually ignore and log unknown fields and version its schema (additive changes, a schema registry), so producers can evolve without breaking consumers.
12. **Strict types: no silent coercion (for example "18" → 18 or 1 → true).** — Coercion would let malformed data pass as valid.
13. **Timestamps must be timezone-aware and are normalized to UTC; naive timestamps are rejected.** — Comparing naive and aware datetimes is a classic bug, and all rule logic assumes one timezone.
14. **null and [] stay distinct and are never replaced with invented values.** Nullable fields: queue_ids (both agent event types), volume_forecast_next_15m, violation_started_at, previous_state, previous_state_duration_sec. — PLAN §3 forbids converting missing data to zero or empty.
15. **The source line number is stored next to the event, not inside it; event_id, ts and line number stay separate.** — They serve different jobs: event_id for dedupe, ts for the clock, line number for file order and diagnostics.
16. **A malformed line is recorded as rejected, with its line number and a readable reason, and loading continues. Phase 1 does not reject duplicates or stale lines.** — One bad record should not hide the rest, and duplicate or stale handling is engine policy (Phase 2).
17. **Blank or whitespace-only lines are rejected with reason "blank line", not skipped.** — Nothing in the input disappears silently; the trailing newline at end of file is not a line.
18. **agent_id (both agent event types) and queue_id (queue_snapshot) must be non-empty, as event_id must.** — Routing and state keys depend on them; an empty ID cannot be attributed to an owner.
19. **No pairing constraint between previous_state and previous_state_duration_sec.** — In the dataset they are always both null (8 first-seen events) or both set, but that is an observed pattern, not a documented schema rule; nulls are preserved as-is.
20. **Nullable fields are still required keys: the key must be present, and its value may be null.** — Keeps "the source said unknown" (null) distinct from "the source omitted it" (missing key), matches the exact key set enforced by `extra="forbid"`, and all 96 dataset records include every key for their type.
