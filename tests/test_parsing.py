"""Structural parsing and validation tests using small synthetic records.

These tests cover schema shape only. Deduplication, freshness, the simulated
clock, state and rules are Phase 2+ and are deliberately not tested here.

Error assertions check that the reason names the offending field (or uses
our own fixed prefixes), never Pydantic's exact wording.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from alerts.ingest import EventParseError, load_events, parse_event
from alerts.models import AdherenceCheck, AgentStateChange, QueueSnapshot


# ---------------------------------------------------------------------------
# Valid base records (shaped like data/events.jsonl). Each test copies one
# and changes a single field.
# ---------------------------------------------------------------------------


def snapshot(**overrides: Any) -> dict[str, Any]:
    record = {
        "event_id": "evt_test_q1",
        "ts": "2026-05-26T09:30:00Z",
        "type": "queue_snapshot",
        "queue_id": "billing",
        "tickets_waiting": 18,
        "longest_wait_sec": 130,
        "sla_target_sec": 120,
        "agents_available": 0,
        "agents_on_call": 4,
        "volume_last_15m": 28,
        "volume_forecast_next_15m": 38,
    }
    return {**record, **overrides}


def state_change(**overrides: Any) -> dict[str, Any]:
    record = {
        "event_id": "evt_test_s1",
        "ts": "2026-05-26T09:30:00Z",
        "type": "agent_state_change",
        "agent_id": "a_31",
        "queue_ids": ["billing", "vip"],
        "previous_state": "available",
        "previous_state_duration_sec": 600,
        "new_state": "on_call",
    }
    return {**record, **overrides}


def adherence(**overrides: Any) -> dict[str, Any]:
    record = {
        "event_id": "evt_test_a1",
        "ts": "2026-05-26T09:36:00Z",
        "type": "adherence_check",
        "agent_id": "a_19",
        "queue_ids": ["billing"],
        "scheduled_state": "available",
        "actual_state": "on_break",
        "in_violation": True,
        "violation_started_at": "2026-05-26T09:35:00Z",
    }
    return {**record, **overrides}


def line(record: dict[str, Any]) -> str:
    return json.dumps(record)


def without(record: dict[str, Any], field: str) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != field}


def write_lines(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("".join(f"{text}\n" for text in lines))
    return path


# ---------------------------------------------------------------------------
# A. Valid input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("record", "expected_class"),
    [
        (snapshot(), QueueSnapshot),
        (state_change(), AgentStateChange),
        (adherence(), AdherenceCheck),
    ],
)
def test_parses_each_event_type(record: dict[str, Any], expected_class: type) -> None:
    event = parse_event(line(record))
    assert isinstance(event, expected_class)
    assert event.event_id == record["event_id"]


def test_z_timestamp_becomes_utc() -> None:
    event = parse_event(line(snapshot(ts="2026-05-26T09:00:00Z")))
    assert event.ts == datetime(2026, 5, 26, 9, 0, tzinfo=UTC)
    assert event.ts.tzinfo is UTC


def test_offset_timestamp_normalized_to_utc() -> None:
    event = parse_event(
        line(
            adherence(
                ts="2026-05-26T11:00:00+02:00",
                violation_started_at="2026-05-26T10:35:00+01:00",
            )
        )
    )
    assert event.ts == datetime(2026, 5, 26, 9, 0, tzinfo=UTC)
    assert event.ts.tzinfo is UTC
    assert event.violation_started_at == datetime(2026, 5, 26, 9, 35, tzinfo=UTC)
    assert event.violation_started_at.tzinfo is UTC


def test_null_and_empty_queue_ids_stay_distinct() -> None:
    null_event = parse_event(line(state_change(queue_ids=None)))
    empty_event = parse_event(line(adherence(queue_ids=[])))
    assert null_event.queue_ids is None
    assert empty_event.queue_ids == []
    assert null_event.queue_ids != empty_event.queue_ids


def test_null_forecast_not_zero() -> None:
    event = parse_event(line(snapshot(volume_forecast_next_15m=None)))
    assert event.volume_forecast_next_15m is None


def test_null_optional_fields_preserved() -> None:
    first_seen = parse_event(
        line(state_change(previous_state=None, previous_state_duration_sec=None))
    )
    assert first_seen.previous_state is None
    assert first_seen.previous_state_duration_sec is None

    unknown_start = parse_event(line(adherence(violation_started_at=None)))
    assert unknown_start.violation_started_at is None


def test_events_are_frozen() -> None:
    event = parse_event(line(snapshot()))
    with pytest.raises(ValidationError):
        event.tickets_waiting = 5


# ---------------------------------------------------------------------------
# B. Rejections: parse_event raises EventParseError naming the field
# ---------------------------------------------------------------------------


def assert_rejected_naming(text: str, field: str) -> None:
    with pytest.raises(EventParseError) as excinfo:
        parse_event(text)
    assert field in excinfo.value.reason


def test_naive_timestamp_rejected() -> None:
    assert_rejected_naming(line(snapshot(ts="2026-05-26T09:00:00")), "ts")


def test_unknown_event_type_rejected() -> None:
    assert_rejected_naming(line(snapshot(type="volume_alert")), "type")


def test_missing_required_field_rejected() -> None:
    assert_rejected_naming(line(without(snapshot(), "tickets_waiting")), "tickets_waiting")


def test_unknown_extra_field_rejected() -> None:
    assert_rejected_naming(line(snapshot(surprise_field=1)), "surprise_field")


@pytest.mark.parametrize(
    ("record", "field"),
    [
        (snapshot(tickets_waiting="18"), "tickets_waiting"),
        (snapshot(tickets_waiting=True), "tickets_waiting"),
        (snapshot(tickets_waiting=18.5), "tickets_waiting"),
        (adherence(in_violation=1), "in_violation"),
    ],
    ids=["str-as-int", "bool-as-int", "float-as-int", "int-as-bool"],
)
def test_no_type_coercion(record: dict[str, Any], field: str) -> None:
    assert_rejected_naming(line(record), field)


# The four agent-state fields, with the builder for the event type that has each one.
STATE_FIELDS = [
    (state_change, "new_state"),
    (state_change, "previous_state"),
    (adherence, "actual_state"),
    (adherence, "scheduled_state"),
]
STATE_FIELD_IDS = [field for _, field in STATE_FIELDS]


@pytest.mark.parametrize("value", ["offline", "lunch"])
@pytest.mark.parametrize(("build", "field"), STATE_FIELDS, ids=STATE_FIELD_IDS)
def test_any_nonempty_agent_state_accepted(build: Any, field: str, value: str) -> None:
    # The assignment lists "available, on_call, on_break, in_meeting, offline, etc."
    event = parse_event(line(build(**{field: value})))
    assert getattr(event, field) == value


@pytest.mark.parametrize("value", ["ON_CALL", " on_call"], ids=["upper-case", "leading-space"])
def test_agent_state_kept_exactly_as_sent(value: str) -> None:
    assert parse_event(line(state_change(new_state=value))).new_state == value


@pytest.mark.parametrize("value", ["", " ", "\t\n"], ids=["empty", "space", "tab-newline"])
@pytest.mark.parametrize(("build", "field"), STATE_FIELDS, ids=STATE_FIELD_IDS)
def test_blank_agent_state_rejected(build: Any, field: str, value: str) -> None:
    assert_rejected_naming(line(build(**{field: value})), field)


@pytest.mark.parametrize(
    ("build", "field"),
    [(state_change, "new_state"), (adherence, "actual_state"), (adherence, "scheduled_state")],
    ids=["new_state", "actual_state", "scheduled_state"],
)
def test_null_agent_state_rejected_where_required(build: Any, field: str) -> None:
    # previous_state stays nullable: see test_null_optional_fields_preserved.
    assert_rejected_naming(line(build(**{field: None})), field)


@pytest.mark.parametrize(
    "value",
    [1, True, ["on_call"], {"state": "on_call"}],
    ids=["int", "bool", "list", "object"],
)
@pytest.mark.parametrize(("build", "field"), STATE_FIELDS, ids=STATE_FIELD_IDS)
def test_non_string_agent_state_rejected(build: Any, field: str, value: Any) -> None:
    assert_rejected_naming(line(build(**{field: value})), field)


@pytest.mark.parametrize(
    ("record", "field"),
    [
        (snapshot(tickets_waiting=-1), "tickets_waiting"),
        (snapshot(longest_wait_sec=-1), "longest_wait_sec"),
        (snapshot(agents_available=-1), "agents_available"),
        (snapshot(agents_on_call=-1), "agents_on_call"),
        (snapshot(volume_last_15m=-1), "volume_last_15m"),
        (snapshot(volume_forecast_next_15m=-1), "volume_forecast_next_15m"),
        (state_change(previous_state_duration_sec=-5), "previous_state_duration_sec"),
    ],
)
def test_negative_numbers_rejected(record: dict[str, Any], field: str) -> None:
    assert_rejected_naming(line(record), field)


def test_zero_sla_target_rejected() -> None:
    assert_rejected_naming(line(snapshot(sla_target_sec=0)), "sla_target_sec")


@pytest.mark.parametrize(
    ("record", "field"),
    [
        (snapshot(event_id=""), "event_id"),
        (snapshot(queue_id=""), "queue_id"),
        (state_change(agent_id=""), "agent_id"),
        (adherence(agent_id=""), "agent_id"),
    ],
)
def test_empty_ids_rejected(record: dict[str, Any], field: str) -> None:
    assert_rejected_naming(line(record), field)


def test_broken_json_rejected() -> None:
    with pytest.raises(EventParseError) as excinfo:
        parse_event('{"event_id": ')
    assert excinfo.value.reason.startswith("invalid JSON")


def test_json_that_is_not_an_object_rejected() -> None:
    with pytest.raises(EventParseError) as excinfo:
        parse_event("[1, 2]")
    assert excinfo.value.reason.startswith("expected a JSON object")


@pytest.mark.parametrize("text", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_blank_line_rejected(text: str) -> None:
    with pytest.raises(EventParseError) as excinfo:
        parse_event(text)
    assert excinfo.value.reason == "blank line"


# ---------------------------------------------------------------------------
# C. load_events on small temporary files
# ---------------------------------------------------------------------------


def test_bad_line_does_not_stop_loading(tmp_path: Path) -> None:
    bad = line(without(snapshot(event_id="evt_bad"), "tickets_waiting"))
    path = write_lines(
        tmp_path,
        [line(snapshot(event_id="evt_1")), bad, line(snapshot(event_id="evt_3"))],
    )

    result = load_events(path)

    assert [s.line_number for s in result.accepted] == [1, 3]
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert rejected.line_number == 2
    assert "tickets_waiting" in rejected.reason
    assert rejected.raw == bad


def test_blank_line_in_file_rejected_not_skipped(tmp_path: Path) -> None:
    path = write_lines(tmp_path, [line(snapshot(event_id="evt_1")), "  ", line(snapshot(event_id="evt_3"))])

    result = load_events(path)

    assert [s.line_number for s in result.accepted] == [1, 3]
    assert [(r.line_number, r.reason) for r in result.rejected] == [(2, "blank line")]


def test_line_number_separate_from_id_and_ts(tmp_path: Path) -> None:
    path = write_lines(
        tmp_path,
        [
            line(snapshot(event_id="evt_first", ts="2026-05-26T10:00:00Z")),
            line(snapshot(event_id="evt_second", ts="2026-05-26T09:00:00Z")),
        ],
    )

    result = load_events(path)

    second = result.accepted[1]
    assert second.line_number == 2
    assert second.event.event_id == "evt_second"
    assert second.event.ts == datetime(2026, 5, 26, 9, 0, tzinfo=UTC)


def test_duplicates_are_not_removed_in_phase1(tmp_path: Path) -> None:
    path = write_lines(
        tmp_path,
        [line(snapshot(event_id="evt_dup")), line(snapshot(event_id="evt_dup"))],
    )

    result = load_events(path)

    assert [s.event.event_id for s in result.accepted] == ["evt_dup", "evt_dup"]
    assert result.rejected == []
