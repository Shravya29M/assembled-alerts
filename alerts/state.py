"""Replay state: dedupe, freshness, the simulated clock and per-subject state.

ReplayState.apply() decides whether an event changes what we believe, and if
so updates the clock and the state for that event's subject. It knows
nothing about rules, incidents or timers.

Each fact has one authoritative event family: queue metrics come only from
queue_snapshot, call state only from agent_state_change, and adherence only
from adherence_check. Disagreements are recorded as diagnostics, never
written across families.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from alerts.ingest import SourcedEvent
from alerts.models import AdherenceCheck, AgentState, AgentStateChange, Event, QueueSnapshot

Outcome = Literal["accepted", "duplicate", "stale"]


@dataclass(frozen=True)
class Diagnostic:
    kind: str
    subject_id: str
    line_number: int
    detail: str


@dataclass(frozen=True)
class CallState:
    agent_id: str
    state: AgentState
    since: datetime  # when the agent entered `state`
    on_call_since: datetime | None  # start of the current call episode, if on a call


@dataclass(frozen=True)
class AdherenceState:
    agent_id: str
    in_violation: bool
    effective_start: datetime | None  # fixed for the whole violation run
    actual_state: AgentState  # evidence only; never overrides CallState
    checked_at: datetime
    reported_start: datetime | None = None  # latest non-null start the source reported in this run


def _subject_id(event: Event) -> str:
    return event.queue_id if isinstance(event, QueueSnapshot) else event.agent_id


class ReplayState:
    def __init__(self) -> None:
        self.clock: datetime | None = None
        self.queues: dict[str, QueueSnapshot] = {}
        self.calls: dict[str, CallState] = {}
        self.adherence: dict[str, AdherenceState] = {}
        self.diagnostics: list[Diagnostic] = []
        self._first_seen: dict[str, Event] = {}  # event_id -> first event with that ID
        self._latest_ts: dict[tuple[str, str], datetime] = {}  # (family, subject) -> ts

    def check(self, sourced: SourcedEvent) -> Outcome:
        """What apply() would do with this event, without changing anything."""
        event = sourced.event
        # 1. Dedupe on event_id, before anything else.
        if event.event_id in self._first_seen:
            return "duplicate"
        # 2. Freshness per (event family, subject). Equal timestamps are fresh.
        latest = self._latest_ts.get((event.type, _subject_id(event)))
        if latest is not None and event.ts < latest:
            return "stale"
        return "accepted"

    def apply(self, sourced: SourcedEvent) -> Outcome:
        """Apply one event in file order and say what happened to it."""
        event, line = sourced.event, sourced.line_number
        outcome = self.check(sourced)

        if outcome == "duplicate":
            first = self._first_seen[event.event_id]
            if first != event:
                self._record("duplicate_payload_differs", event.event_id, line,
                             f"first seen with ts {first.ts.isoformat()}, now {event.ts.isoformat()}")
            return outcome
        self._first_seen[event.event_id] = event  # marked seen even if stale (Decision 21)
        if outcome == "stale":
            return outcome
        self._latest_ts[(event.type, _subject_id(event))] = event.ts

        # 3. Monotonic clock: C = max(C, ts). Never rewinds.
        self.clock = event.ts if self.clock is None else max(self.clock, event.ts)

        # 4. Update the state owned by this event's family.
        if isinstance(event, QueueSnapshot):
            self._apply_snapshot(event, line)
        elif isinstance(event, AgentStateChange):
            self._apply_state_change(event, line)
        else:
            self._apply_adherence(event, line)
        return "accepted"

    def _apply_snapshot(self, event: QueueSnapshot, line: int) -> None:
        self.queues[event.queue_id] = event
        if event.volume_forecast_next_15m is None:
            self._record("forecast_missing", event.queue_id, line, "volume_forecast_next_15m is null")

    def _apply_state_change(self, event: AgentStateChange, line: int) -> None:
        current = self.calls.get(event.agent_id)
        reported = event.previous_state_duration_sec
        if current is not None and reported is not None:
            derived = int((event.ts - current.since).total_seconds())
            if derived != reported:
                self._record("duration_mismatch", event.agent_id, line,
                             f"reported {reported}s, derived {derived}s from accepted transitions")

        if current is not None and current.state == event.new_state:
            return  # repeated state (e.g. on_call -> on_call): not a new transition

        self.calls[event.agent_id] = CallState(
            agent_id=event.agent_id,
            state=event.new_state,
            since=event.ts,
            on_call_since=event.ts if event.new_state == "on_call" else None,
        )

    def _apply_adherence(self, event: AdherenceCheck, line: int) -> None:
        current = self.adherence.get(event.agent_id)
        reported = event.violation_started_at

        if not event.in_violation:
            start, last_reported = None, None
        elif current is not None and current.in_violation:
            start = current.effective_start  # same run: the start never moves
            last_reported = current.reported_start
            if reported is not None and last_reported is not None and reported != last_reported:
                self._record("adherence_start_changed", event.agent_id, line,
                             f"source reported {last_reported.isoformat()}, now {reported.isoformat()}; "
                             f"run start {start.isoformat()} kept")
            if reported is not None:
                last_reported = reported
        else:
            last_reported = reported
            if reported is not None and reported <= event.ts:
                start = reported  # new run with a usable reported start
            else:
                start = event.ts  # new run: null or future start, so use first observation

        self.adherence[event.agent_id] = AdherenceState(
            agent_id=event.agent_id,
            in_violation=event.in_violation,
            effective_start=start,
            actual_state=event.actual_state,
            checked_at=event.ts,
            reported_start=last_reported,
        )

        call = self.calls.get(event.agent_id)
        if call is not None and call.state != event.actual_state:
            self._record("conflicting_activity", event.agent_id, line,
                         f"adherence says {event.actual_state}; authoritative state is {call.state}")

    def _record(self, kind: str, subject_id: str, line: int, detail: str) -> None:
        self.diagnostics.append(Diagnostic(kind, subject_id, line, detail))
