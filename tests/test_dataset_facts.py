"""Facts about data/events.jsonl, frozen as tests.

Every expected value here was checked directly against the file. These tests
only parse; they do not dedupe, check freshness or evaluate rules.
"""

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alerts.ingest import LoadResult, load_events
from alerts.models import AdherenceCheck, AgentStateChange, Event, QueueSnapshot

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"

ALLOWED_STATES = {"available", "on_call", "on_break", "in_meeting"}


@pytest.fixture(scope="module")
def result() -> LoadResult:
    return load_events(DATA_PATH)


@pytest.fixture(scope="module")
def by_line(result: LoadResult) -> dict[int, Event]:
    return {sourced.line_number: sourced.event for sourced in result.accepted}


def test_all_96_lines_parse(result: LoadResult) -> None:
    assert len(result.accepted) == 96
    assert result.rejected == []


def test_event_type_counts(result: LoadResult) -> None:
    counts = Counter(sourced.event.type for sourced in result.accepted)
    assert counts == {"queue_snapshot": 24, "agent_state_change": 31, "adherence_check": 41}


def test_file_order_preserved(result: LoadResult) -> None:
    assert [sourced.line_number for sourced in result.accepted] == list(range(1, 97))


def test_duplicate_event_id_has_differing_ts(result: LoadResult, by_line: dict[int, Event]) -> None:
    lines_with_050 = [
        s.line_number for s in result.accepted if s.event.event_id == "evt_01HXYZ050"
    ]
    assert lines_with_050 == [50, 95]
    assert by_line[50].ts == datetime(2026, 5, 26, 9, 36, 0, tzinfo=UTC)
    assert by_line[95].ts == datetime(2026, 5, 26, 9, 36, 30, tzinfo=UTC)
    assert len({s.event.event_id for s in result.accepted}) == 95


def test_stale_vip_snapshot_is_last_line(by_line: dict[int, Event]) -> None:
    latest_vip, stale_vip = by_line[93], by_line[96]
    assert isinstance(latest_vip, QueueSnapshot) and isinstance(stale_vip, QueueSnapshot)
    assert latest_vip.queue_id == stale_vip.queue_id == "vip"
    assert latest_vip.ts == datetime(2026, 5, 26, 10, 30, tzinfo=UTC)
    assert stale_vip.ts == datetime(2026, 5, 26, 9, 49, tzinfo=UTC)


def test_billing_forecast_null_at_1000(by_line: dict[int, Event]) -> None:
    event = by_line[68]
    assert isinstance(event, QueueSnapshot)
    assert event.queue_id == "billing"
    assert event.ts == datetime(2026, 5, 26, 10, 0, tzinfo=UTC)
    assert event.volume_forecast_next_15m is None


def test_a05_null_then_empty_queue_ids(by_line: dict[int, Event]) -> None:
    null_queues, empty_queues = by_line[73], by_line[74]
    assert isinstance(null_queues, AgentStateChange) and isinstance(empty_queues, AdherenceCheck)
    assert null_queues.agent_id == empty_queues.agent_id == "a_05"
    assert null_queues.queue_ids is None
    assert empty_queues.queue_ids == []


def test_a23_violation_with_null_start(by_line: dict[int, Event]) -> None:
    event = by_line[86]
    assert isinstance(event, AdherenceCheck)
    assert event.agent_id == "a_23"
    assert event.in_violation is True
    assert event.violation_started_at is None


def test_a31_reported_duration_differs_from_timestamps(by_line: dict[int, Event]) -> None:
    call_start, call_end = by_line[38], by_line[90]
    assert isinstance(call_start, AgentStateChange) and isinstance(call_end, AgentStateChange)
    assert call_start.agent_id == call_end.agent_id == "a_31"
    assert call_start.new_state == call_end.previous_state == "on_call"
    assert call_end.previous_state_duration_sec == 3900
    assert (call_end.ts - call_start.ts).total_seconds() == 3300


def test_first_seen_agents_have_null_previous_fields(by_line: dict[int, Event]) -> None:
    first_seen = [by_line[n] for n in range(4, 12)]
    assert all(isinstance(e, AgentStateChange) for e in first_seen)
    assert len({e.agent_id for e in first_seen}) == 8
    assert all(e.previous_state is None for e in first_seen)
    assert all(e.previous_state_duration_sec is None for e in first_seen)


def test_observed_states_are_exactly_the_allowed_set(result: LoadResult) -> None:
    observed: set[str] = set()
    for sourced in result.accepted:
        event = sourced.event
        if isinstance(event, AgentStateChange):
            observed.add(event.new_state)
            if event.previous_state is not None:
                observed.add(event.previous_state)
        elif isinstance(event, AdherenceCheck):
            observed.update({event.scheduled_state, event.actual_state})
    assert observed == ALLOWED_STATES


def test_all_timestamps_are_utc(result: LoadResult) -> None:
    for sourced in result.accepted:
        event = sourced.event
        assert event.ts.tzinfo is UTC
        if isinstance(event, AdherenceCheck) and event.violation_started_at is not None:
            assert event.violation_started_at.tzinfo is UTC
