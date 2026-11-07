"""Phase 2 replay of the real dataset through ReplayState and the four rules.

Every expected value was computed independently from data/events.jsonl.
These tests check state and rule conditions, not notifications or timers
(Phase 3).
"""

from datetime import datetime
from pathlib import Path

import pytest

from alerts.ingest import SourcedEvent, load_events
from alerts.models import QueueSnapshot
from alerts.rules import AdherenceRule, BacklogRule, Decision, LongCallRule, SlaRule, evaluate
from alerts.state import ReplayState
from tests.builders import at, diagnostic_keys

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"
EVENTS: list[SourcedEvent] = load_events(DATA_PATH).accepted
AGENTS = ["a_05", "a_07", "a_11", "a_19", "a_23", "a_31", "a_42", "a_88"]


def replay(until_line: int | None = None) -> tuple[ReplayState, dict[int, str]]:
    """Apply dataset lines in file order, up to and including until_line."""
    state = ReplayState()
    outcomes: dict[int, str] = {}
    for sourced in EVENTS:
        if until_line is not None and sourced.line_number > until_line:
            break
        outcomes[sourced.line_number] = state.apply(sourced)
    return state, outcomes


@pytest.fixture(scope="module")
def full() -> tuple[ReplayState, dict[int, str]]:
    return replay()


def queue_conditions(rule: BacklogRule | SlaRule, queue_id: str) -> list[tuple[int, str]]:
    """(line, condition) right after each accepted snapshot for queue_id."""
    state = ReplayState()
    results = []
    for sourced in EVENTS:
        outcome = state.apply(sourced)
        event = sourced.event
        if outcome == "accepted" and isinstance(event, QueueSnapshot) and event.queue_id == queue_id:
            decision = evaluate(rule, state.queues[queue_id], state.clock)
            results.append((sourced.line_number, decision.condition))
    return results


# ---------------------------------------------------------------------------
# Outcomes, clock, diagnostics
# ---------------------------------------------------------------------------


def test_outcomes(full: tuple[ReplayState, dict[int, str]]) -> None:
    _, outcomes = full
    assert [line for line, o in outcomes.items() if o == "duplicate"] == [95]
    assert [line for line, o in outcomes.items() if o == "stale"] == [96]
    assert sum(o == "accepted" for o in outcomes.values()) == 94


def test_clock_is_monotonic_and_ends_at_1030() -> None:
    state = ReplayState()
    clocks: dict[int, datetime] = {}
    for sourced in EVENTS:
        state.apply(sourced)
        clocks[sourced.line_number] = state.clock
    values = list(clocks.values())
    assert all(earlier <= later for earlier, later in zip(values, values[1:]))
    assert clocks[94] == clocks[95] == clocks[96] == at("10:30")


def test_exact_diagnostics(full: tuple[ReplayState, dict[int, str]]) -> None:
    state, _ = full
    assert diagnostic_keys(state.diagnostics) == [
        ("forecast_missing", "billing", 68),
        ("conflicting_activity", "a_23", 86),
        ("duration_mismatch", "a_31", 90),
        ("duplicate_payload_differs", "evt_01HXYZ050", 95),
    ]


def test_vip_ignores_stale_snapshot(full: tuple[ReplayState, dict[int, str]]) -> None:
    state, _ = full
    vip = state.queues["vip"]
    assert vip.event_id == "evt_01HXYZ093"
    assert vip.ts == at("10:30")


# ---------------------------------------------------------------------------
# Queue rules over the replay
# ---------------------------------------------------------------------------


def test_billing_backlog_sequence() -> None:
    # tickets_waiting: 0, 1, 5, 12, 18, 22, 20, 19, 17, 14, 8, 9, 3
    assert queue_conditions(BacklogRule(rule_id="backlog", scope="all_owned"), "billing") == [
        (1, "clear"), (12, "clear"), (20, "clear"), (36, "clear"),
        (39, "warning"), (49, "critical"), (52, "warning"), (55, "warning"),
        (66, "warning"), (68, "clear"), (77, "clear"), (87, "clear"), (91, "clear"),
    ]  # fmt: skip


def test_billing_sla_sequence() -> None:
    # longest_wait/120: 0, 15, 90 (exactly 0.75), 90, 130, 180, 240, 260, 270, 200, 90, 110, 30
    assert queue_conditions(SlaRule(rule_id="sla", scope="all_owned"), "billing") == [
        (1, "clear"), (12, "clear"), (20, "clear"), (36, "clear"),
        (39, "critical"), (49, "critical"), (52, "critical"), (55, "critical"),
        (66, "critical"), (68, "critical"), (77, "clear"), (87, "warning"), (91, "clear"),
    ]  # fmt: skip


def test_tier2_sla_sequence() -> None:
    # longest_wait/300: 0, 0, 60, 250, 320, 180, 60
    assert queue_conditions(SlaRule(rule_id="sla", scope="all_owned"), "tier_2") == [
        (2, "clear"), (13, "clear"), (21, "clear"), (54, "warning"),
        (69, "critical"), (78, "clear"), (92, "clear"),
    ]  # fmt: skip


# ---------------------------------------------------------------------------
# Agent checkpoints during the replay
# ---------------------------------------------------------------------------


def test_a11_long_call_met_exactly_at_0955() -> None:
    state, _ = replay(until_line=67)  # line 67 is at 09:55
    assert state.clock == at("09:55")
    decision = evaluate(LongCallRule(rule_id="long_call", scope="all_owned"), state.calls["a_11"], state.clock)
    assert decision == Decision("met", at("09:55"))


def test_a19_still_in_violation_after_state_change_at_1010() -> None:
    state, _ = replay(until_line=75)  # line 75: a_19 on_break -> available at 10:10:00
    decision = evaluate(AdherenceRule(rule_id="adherence", scope="all_owned"), state.adherence["a_19"], state.clock)
    assert decision == Decision("met", at("09:45"))


def test_a19_cleared_by_adherence_check_at_101030() -> None:
    state, _ = replay(until_line=76)  # line 76: in_violation=false at 10:10:30
    decision = evaluate(AdherenceRule(rule_id="adherence", scope="all_owned"), state.adherence["a_19"], state.clock)
    assert decision == Decision("clear")


def test_a23_call_kept_after_conflicting_check() -> None:
    state, _ = replay(until_line=86)  # line 86: adherence says in_meeting
    assert state.calls["a_23"].state == "on_call"
    assert state.calls["a_23"].on_call_since == at("09:45")


# ---------------------------------------------------------------------------
# End of replay (clock 10:30)
# ---------------------------------------------------------------------------


def test_long_calls_at_end(full: tuple[ReplayState, dict[int, str]]) -> None:
    state, _ = full
    rule = LongCallRule(rule_id="long_call", scope="all_owned")
    decisions = {agent: evaluate(rule, state.calls[agent], state.clock) for agent in AGENTS}
    assert decisions == {
        "a_05": Decision("clear"),
        "a_07": Decision("met", at("10:10")),
        "a_11": Decision("clear"),
        "a_19": Decision("clear"),
        "a_23": Decision("met", at("10:30")),  # deadline exactly at the horizon
        "a_31": Decision("clear"),
        "a_42": Decision("pending", at("10:33")),  # beyond the horizon
        "a_88": Decision("clear"),
    }


def test_adherence_at_end(full: tuple[ReplayState, dict[int, str]]) -> None:
    state, _ = full
    rule = AdherenceRule(rule_id="adherence", scope="all_owned")
    decisions = {agent: evaluate(rule, state.adherence[agent], state.clock) for agent in AGENTS}
    assert decisions == {
        "a_05": Decision("clear"),
        "a_07": Decision("clear"),
        "a_11": Decision("clear"),
        "a_19": Decision("clear"),
        "a_23": Decision("met", at("10:25:30")),  # null start: first observed 10:15:30
        "a_31": Decision("clear"),
        "a_42": Decision("clear"),
        "a_88": Decision("met", at("10:10")),
    }


def test_a05_null_and_empty_queue_ids_accepted(full: tuple[ReplayState, dict[int, str]]) -> None:
    state, outcomes = full
    assert outcomes[73] == outcomes[74] == "accepted"
    assert not [d for d in state.diagnostics if d.subject_id == "a_05"]


def test_replay_is_deterministic() -> None:
    first_state, first_outcomes = replay()
    second_state, second_outcomes = replay()
    assert first_outcomes == second_outcomes
    assert first_state.diagnostics == second_state.diagnostics
    rule = LongCallRule(rule_id="long_call", scope="all_owned")
    assert [evaluate(rule, first_state.calls[a], first_state.clock) for a in AGENTS] == [
        evaluate(rule, second_state.calls[a], second_state.clock) for a in AGENTS
    ]
