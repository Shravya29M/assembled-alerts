"""Small builders for synthetic events and replays, shared by the Phase 2 tests.

Events are built with the real Phase 1 models, so every synthetic event is
valid input. All times are on 2026-05-26 in UTC, like the dataset.
"""

from datetime import UTC, datetime
from itertools import count

from alerts.ingest import SourcedEvent
from alerts.models import AdherenceCheck, AgentStateChange, Event, QueueSnapshot
from alerts.state import Diagnostic, ReplayState

_ids = count(1)


def at(clock_time: str) -> datetime:
    """'09:30' or '09:30:15' -> that time on 2026-05-26 UTC."""
    parts = [int(p) for p in clock_time.split(":")]
    hour, minute, second = (parts + [0])[:3]
    return datetime(2026, 5, 26, hour, minute, second, tzinfo=UTC)


def _new_id(event_id: str | None) -> str:
    return event_id if event_id is not None else f"evt_syn_{next(_ids)}"


def snapshot(
    ts: str,
    queue_id: str = "billing",
    *,
    tickets_waiting: int = 0,
    longest_wait_sec: int = 0,
    sla_target_sec: int = 120,
    forecast: int | None = 10,
    event_id: str | None = None,
) -> QueueSnapshot:
    return QueueSnapshot(
        event_id=_new_id(event_id),
        ts=at(ts),
        type="queue_snapshot",
        queue_id=queue_id,
        tickets_waiting=tickets_waiting,
        longest_wait_sec=longest_wait_sec,
        sla_target_sec=sla_target_sec,
        agents_available=1,
        agents_on_call=1,
        volume_last_15m=5,
        volume_forecast_next_15m=forecast,
    )


def state_change(
    ts: str,
    agent_id: str,
    new_state: str,
    *,
    previous_state: str | None = None,
    duration_sec: int | None = None,
    event_id: str | None = None,
) -> AgentStateChange:
    return AgentStateChange(
        event_id=_new_id(event_id),
        ts=at(ts),
        type="agent_state_change",
        agent_id=agent_id,
        queue_ids=["billing"],
        previous_state=previous_state,
        previous_state_duration_sec=duration_sec,
        new_state=new_state,
    )


def adherence(
    ts: str,
    agent_id: str,
    *,
    in_violation: bool,
    started: str | None = None,
    actual_state: str = "on_break",
    event_id: str | None = None,
) -> AdherenceCheck:
    return AdherenceCheck(
        event_id=_new_id(event_id),
        ts=at(ts),
        type="adherence_check",
        agent_id=agent_id,
        queue_ids=["billing"],
        scheduled_state="available",
        actual_state=actual_state,
        in_violation=in_violation,
        violation_started_at=at(started) if started is not None else None,
    )


def run(*events: Event) -> tuple[ReplayState, list[str]]:
    """Apply events to a fresh ReplayState; event N gets line number N."""
    state = ReplayState()
    outcomes = [
        state.apply(SourcedEvent(line_number, event))
        for line_number, event in enumerate(events, start=1)
    ]
    return state, outcomes


def diagnostic_keys(diagnostics: list[Diagnostic]) -> list[tuple[str, str, int]]:
    """(kind, subject_id, line_number) for each diagnostic; ignores wording."""
    return [(d.kind, d.subject_id, d.line_number) for d in diagnostics]
