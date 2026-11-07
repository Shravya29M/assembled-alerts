"""Phase 2 state management: dedupe, freshness, clock, episodes, diagnostics.

Synthetic events only. Each test applies a few events to a fresh ReplayState
and checks outcomes, state and diagnostics.
"""

import pytest

from alerts.rules import BacklogRule, evaluate
from tests.builders import (
    adherence,
    at,
    diagnostic_keys,
    run,
    snapshot,
    state_change,
)

# ---------------------------------------------------------------------------
# A1. Deduplication
# ---------------------------------------------------------------------------


def test_accepted_event_updates_state_and_clock() -> None:
    event = snapshot("09:30", tickets_waiting=5)
    state, outcomes = run(event)
    assert outcomes == ["accepted"]
    assert state.clock == at("09:30")
    assert state.queues["billing"] == event


def test_duplicate_id_is_ignored() -> None:
    first = snapshot("09:30", tickets_waiting=5, event_id="evt_x")
    second = snapshot("09:31", tickets_waiting=30, event_id="evt_x")
    state, outcomes = run(first, second)
    assert outcomes == ["accepted", "duplicate"]
    assert state.queues["billing"] == first
    assert state.clock == at("09:30")


def test_duplicate_with_different_payload_is_flagged() -> None:
    state, _ = run(
        snapshot("09:36", event_id="evt_x"),
        snapshot("09:36:30", event_id="evt_x"),
    )
    assert diagnostic_keys(state.diagnostics) == [("duplicate_payload_differs", "evt_x", 2)]


def test_identical_duplicate_is_not_flagged() -> None:
    event = snapshot("09:36", event_id="evt_x")
    state, outcomes = run(event, event)
    assert outcomes == ["accepted", "duplicate"]
    assert state.diagnostics == []


def test_dedupe_runs_before_freshness() -> None:
    stale = snapshot("09:50", event_id="evt_old")
    _, outcomes = run(snapshot("10:00"), stale, stale)
    assert outcomes == ["accepted", "stale", "duplicate"]


# ---------------------------------------------------------------------------
# A2. Freshness: per (event family, subject)
# ---------------------------------------------------------------------------


def test_older_snapshot_for_same_queue_is_stale() -> None:
    newer = snapshot("10:00", tickets_waiting=3)
    state, outcomes = run(newer, snapshot("09:50", tickets_waiting=4))
    assert outcomes == ["accepted", "stale"]
    assert state.queues["billing"] == newer
    assert state.clock == at("10:00")


def test_stale_event_that_would_alert_is_rejected() -> None:
    state, outcomes = run(
        snapshot("10:00", tickets_waiting=3),
        snapshot("09:50", tickets_waiting=30),  # would be backlog critical
    )
    assert outcomes == ["accepted", "stale"]
    rule = BacklogRule(rule_id="backlog", scope="all_owned", recipient="lead_billing")
    assert evaluate(rule, state.queues["billing"], state.clock).condition == "clear"


def test_equal_timestamp_same_subject_later_line_wins() -> None:
    state, outcomes = run(
        snapshot("10:00", tickets_waiting=8),
        snapshot("10:00", tickets_waiting=12),
    )
    assert outcomes == ["accepted", "accepted"]
    assert state.queues["billing"].tickets_waiting == 12


def test_freshness_is_per_subject() -> None:
    _, outcomes = run(snapshot("10:00", "billing"), snapshot("09:50", "tier_2"))
    assert outcomes == ["accepted", "accepted"]


def test_freshness_is_per_family() -> None:
    _, outcomes = run(
        state_change("10:00", "a_19", "available"),
        adherence("09:55", "a_19", in_violation=False, actual_state="available"),
    )
    assert outcomes == ["accepted", "accepted"]


def test_stale_exit_does_not_end_call() -> None:
    state, outcomes = run(
        state_change("10:00", "a_11", "on_call"),
        state_change("09:50", "a_11", "available", previous_state="on_call"),
    )
    assert outcomes == ["accepted", "stale"]
    assert state.calls["a_11"].on_call_since == at("10:00")


# ---------------------------------------------------------------------------
# A3. Simulated clock: C = max(C, ts) over accepted events only
# ---------------------------------------------------------------------------


def test_clock_starts_empty() -> None:
    state, _ = run()
    assert state.clock is None


def test_late_but_fresh_event_does_not_rewind_clock() -> None:
    state, outcomes = run(snapshot("10:00", "billing"), snapshot("09:50", "tier_2"))
    assert outcomes == ["accepted", "accepted"]
    assert state.clock == at("10:00")
    assert state.queues["tier_2"].ts == at("09:50")


def test_duplicate_and_stale_events_never_move_clock() -> None:
    state, outcomes = run(
        snapshot("10:00", event_id="evt_a"),
        snapshot("10:30", event_id="evt_a"),  # duplicate with a later ts
        snapshot("09:00"),  # stale
    )
    assert outcomes == ["accepted", "duplicate", "stale"]
    assert state.clock == at("10:00")


# ---------------------------------------------------------------------------
# A4. Call episodes (agent_state_change is authoritative)
# ---------------------------------------------------------------------------


def test_on_call_starts_episode_at_event_ts() -> None:
    state, _ = run(state_change("09:10", "a_11", "on_call"))
    assert state.calls["a_11"].on_call_since == at("09:10")


def test_exit_ends_episode() -> None:
    state, _ = run(
        state_change("09:10", "a_11", "on_call"),
        state_change("10:20", "a_11", "available", previous_state="on_call", duration_sec=4200),
    )
    assert state.calls["a_11"].on_call_since is None
    assert state.calls["a_11"].state == "available"


def test_repeated_on_call_does_not_restart_episode() -> None:
    state, _ = run(
        state_change("09:10", "a_11", "on_call"),
        state_change("09:30", "a_11", "on_call", previous_state="on_call", duration_sec=1200),
    )
    assert state.calls["a_11"].on_call_since == at("09:10")


def test_duration_mismatch_is_flagged() -> None:
    state, _ = run(
        state_change("09:30", "a_31", "on_call"),
        state_change("10:25", "a_31", "available", previous_state="on_call", duration_sec=3900),
    )
    assert diagnostic_keys(state.diagnostics) == [("duration_mismatch", "a_31", 2)]


def test_matching_duration_is_not_flagged() -> None:
    state, _ = run(
        state_change("09:30", "a_31", "on_call"),
        state_change("10:25", "a_31", "available", previous_state="on_call", duration_sec=3300),
    )
    assert state.diagnostics == []


@pytest.mark.parametrize(
    ("previous_state", "duration_sec"),
    [(None, None), ("available", 600)],
    ids=["null-previous", "reported-previous"],
)
def test_first_seen_agent_is_not_flagged(previous_state: str | None, duration_sec: int | None) -> None:
    state, _ = run(
        state_change("09:00", "a_31", "available", previous_state=previous_state, duration_sec=duration_sec)
    )
    assert state.diagnostics == []


# ---------------------------------------------------------------------------
# A5. Adherence runs (adherence_check is authoritative)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check_ts", "started", "expected_start"),
    [
        ("09:36", "09:35", "09:35"),
        ("10:15:30", None, "10:15:30"),
        ("09:36", "09:40", "09:36"),
    ],
    ids=["reported-start", "null-start-uses-check-ts", "future-start-falls-back"],
)
def test_effective_violation_start(check_ts: str, started: str | None, expected_start: str) -> None:
    state, _ = run(adherence(check_ts, "a_19", in_violation=True, started=started))
    assert state.adherence["a_19"].in_violation is True
    assert state.adherence["a_19"].effective_start == at(expected_start)


def test_start_is_fixed_for_the_whole_run() -> None:
    state, _ = run(
        adherence("09:36", "a_19", in_violation=True, started="09:35"),
        adherence("09:45", "a_19", in_violation=True, started="09:38"),
    )
    assert state.adherence["a_19"].effective_start == at("09:35")
    assert diagnostic_keys(state.diagnostics) == [("adherence_start_changed", "a_19", 2)]


def test_same_future_start_reported_twice_is_not_a_change() -> None:
    state, _ = run(
        adherence("09:36", "a_19", in_violation=True, started="09:40"),  # future: falls back to 09:36
        adherence("09:45", "a_19", in_violation=True, started="09:40"),  # same report again
    )
    assert state.adherence["a_19"].effective_start == at("09:36")
    assert state.diagnostics == []


def test_false_check_clears_run() -> None:
    state, _ = run(
        adherence("09:36", "a_19", in_violation=True, started="09:35"),
        adherence("10:10:30", "a_19", in_violation=False, actual_state="available"),
    )
    assert state.adherence["a_19"].in_violation is False
    assert state.adherence["a_19"].effective_start is None


def test_new_run_gets_new_start() -> None:
    state, _ = run(
        adherence("09:36", "a_19", in_violation=True, started="09:35"),
        adherence("10:10:30", "a_19", in_violation=False, actual_state="available"),
        adherence("10:20", "a_19", in_violation=True, started="10:19"),
    )
    assert state.adherence["a_19"].effective_start == at("10:19")
    assert state.diagnostics == []


def test_state_change_does_not_clear_adherence() -> None:
    state, _ = run(
        adherence("09:36", "a_19", in_violation=True, started="09:35"),
        state_change("10:10", "a_19", "available", previous_state="on_break", duration_sec=2100),
    )
    assert state.adherence["a_19"].in_violation is True
    assert state.adherence["a_19"].effective_start == at("09:35")


# ---------------------------------------------------------------------------
# A6. Conflicts between event families, and missing data
# ---------------------------------------------------------------------------


def test_conflicting_activity_is_recorded_and_call_state_kept() -> None:
    state, outcomes = run(
        state_change("09:45", "a_23", "on_call"),
        adherence("10:15:30", "a_23", in_violation=True, actual_state="in_meeting"),
    )
    assert outcomes == ["accepted", "accepted"]
    assert diagnostic_keys(state.diagnostics) == [("conflicting_activity", "a_23", 2)]
    assert state.calls["a_23"].state == "on_call"
    assert state.calls["a_23"].on_call_since == at("09:45")


def test_matching_activity_is_not_flagged() -> None:
    state, _ = run(
        state_change("09:45", "a_23", "on_call"),
        adherence("10:00", "a_23", in_violation=False, actual_state="on_call"),
    )
    assert state.diagnostics == []


def test_unknown_call_state_is_not_a_conflict() -> None:
    state, _ = run(adherence("10:00", "a_23", in_violation=True, actual_state="in_meeting"))
    assert state.diagnostics == []


def test_missing_forecast_is_flagged_and_snapshot_kept() -> None:
    event = snapshot("10:00", tickets_waiting=14, forecast=None)
    state, outcomes = run(event)
    assert outcomes == ["accepted"]
    assert state.queues["billing"] == event
    assert diagnostic_keys(state.diagnostics) == [("forecast_missing", "billing", 1)]
