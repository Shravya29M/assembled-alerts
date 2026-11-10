"""Phase 5.3 — creating rules (Engine.add_rule, Engine.new_rule_id).

A created rule starts at version 1, never notifies when it is added, and
works prospectively: queue rules from the next snapshot, duration rules
through the same timer path as an edit (max(due, clock), Decision 45).
After finish() it schedules nothing until reset() (Decisions 66, 68).

Expected rows were computed independently with a scratch prototype and
checked against data/events.jsonl. In every dataset scenario the seeded
rules' rows must still equal ORACLE exactly.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from alerts.engine import Engine
from alerts.incidents import Notification
from alerts.routing import RoutingError, seeded_rules
from alerts.rules import AdherenceRule, AgentScope, BacklogRule, LongCallRule, QueueScope, Rule
from tests.builders import at, snapshot, state_change
from tests.engine_builders import Driver, brief, engine_snapshot, hm
from tests.test_dataset_oracle import EVENTS, ORACLE

# ---------------------------------------------------------------------------
# Rules used by the scenarios (IDs are what new_rule_id must generate)
# ---------------------------------------------------------------------------

# R1: the assignment's own example, "tell me when billing has more than 20 tickets waiting".
R1 = BacklogRule(rule_id="backlog.lead_billing.2", recipient="lead_billing",
                 scope=QueueScope(queues=["billing"]), warning_above=20, critical_above=25)  # fmt: skip
# R2: a_19 out of adherence for 5 minutes.
R2 = AdherenceRule(rule_id="adherence.lead_billing.2", recipient="lead_billing",
                   scope=AgentScope(agents=["a_19"]), min_duration_sec=300)  # fmt: skip
# R3: a_11 on one call for 30 minutes.
R3 = LongCallRule(rule_id="long_call.lead_support.2", recipient="lead_support",
                  scope=AgentScope(agents=["a_11"]), min_duration_sec=1800)  # fmt: skip
# R4: every lead_support agent on one call for 30 minutes.
R4 = LongCallRule(rule_id="long_call.lead_support.2", recipient="lead_support", scope="all_owned",
                  min_duration_sec=1800)  # fmt: skip

# (seq, line, rule_id, subject, kind, severity, effective, emitted, rule_version)
R1_NEW_ROWS = [
    (4, 49, "backlog.lead_billing.2", "billing", "opened", "warning", "09:36", "09:36", 1),
    (5, 52, "backlog.lead_billing.2", "billing", "resolved", "warning", "09:40", "09:40", 1),
]
R2_NEW_ROWS = [
    (4, 53, "adherence.lead_billing.2", "a_19", "opened", None, "09:40", "09:45", 1),
    (13, 76, "adherence.lead_billing.2", "a_19", "resolved", None, "10:10:30", "10:10:30", 1),
]
R3_NEW_ROWS = [
    (6, 64, "long_call.lead_support.2", "a_11", "opened", None, "09:45", "09:48", 1),
    (18, 89, "long_call.lead_support.2", "a_11", "resolved", None, "10:20", "10:20", 1),
]
R4_NEW_ROWS = [
    (4, 51, "long_call.lead_support.2", "a_42", "opened", None, "09:37", "09:38", 1),
    (5, 51, "long_call.lead_support.2", "a_42", "resolved", None, "09:38", "09:38", 1),  # same step
    (6, 53, "long_call.lead_support.2", "a_11", "opened", None, "09:40", "09:45", 1),
    (18, 86, "long_call.lead_support.2", "a_23", "opened", None, "10:15", "10:15:30", 1),
    (20, 89, "long_call.lead_support.2", "a_42", "opened", None, "10:18", "10:20", 1),
    (22, 89, "long_call.lead_support.2", "a_11", "resolved", None, "10:20", "10:20", 1),
]
R4_TOTAL = 26
R4_ACTIVE = [
    ("adherence.lead_support", "a_23"), ("adherence.lead_support", "a_88"),
    ("long_call.lead_billing", "a_07"), ("long_call.lead_support", "a_23"),
    ("long_call.lead_support.2", "a_23"), ("long_call.lead_support.2", "a_42"),
]  # fmt: skip


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def rows(notifications: list[Notification]) -> list[tuple[Any, ...]]:
    return [
        (n.seq, n.line_number, n.rule_id, n.subject_id, n.kind, n.severity,
         hm(n.effective_at), hm(n.emitted_at), n.rule_version)
        for n in notifications
    ]  # fmt: skip


def oracle_shape(notifications: list[Notification]) -> list[tuple[Any, ...]]:
    """Same tuple shape as ORACLE."""
    return [
        (hm(n.emitted_at), hm(n.effective_at), n.rule_id, n.subject_id, n.kind, n.severity, n.recipient)
        for n in notifications
    ]


def split(notifications: list[Notification], new_rule_id: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """(rows of the added rule, ORACLE-shaped rows of every other rule)."""
    new = [n for n in notifications if n.rule_id == new_rule_id]
    others = [n for n in notifications if n.rule_id != new_rule_id]
    return rows(new), oracle_shape(others)


def replay(engine: Engine, after_line: int, upto_line: int) -> None:
    for sourced in EVENTS:
        if after_line < sourced.line_number <= upto_line:
            engine.step(sourced)


def run_with_rule_added_after(line: int, rule: Rule) -> Engine:
    """Replay the dataset, adding `rule` once line `line` is processed (0 = before any event)."""
    engine = Engine(seeded_rules())
    replay(engine, 0, line)
    engine.add_rule(rule)
    replay(engine, line, 10_000)
    engine.finish()
    return engine


# ---------------------------------------------------------------------------
# E1-E3, E11: synthetic
# ---------------------------------------------------------------------------


def test_new_rule_ids_are_deterministic() -> None:
    engine = Engine(seeded_rules())
    before = engine_snapshot(engine)
    assert engine.new_rule_id("backlog", "lead_billing") == "backlog.lead_billing.2"
    assert engine.new_rule_id("long_call", "lead_support") == "long_call.lead_support.2"
    assert engine_snapshot(engine) == before  # generating an ID changes nothing

    engine.add_rule(R1)
    assert engine.new_rule_id("backlog", "lead_billing") == "backlog.lead_billing.3"
    assert engine.new_rule_id("long_call", "lead_support") == "long_call.lead_support.2"


def test_added_rule_has_version_1_and_never_notifies() -> None:
    driver = Driver()
    driver.send(snapshot("09:30", "billing", tickets_waiting=30))  # R1 would be critical now
    before = engine_snapshot(driver.engine)
    assert driver.engine.add_rule(R1) == []
    assert driver.engine.rules[R1.rule_id] == R1
    assert driver.engine.rule_version(R1.rule_id) == 1
    after = engine_snapshot(driver.engine)
    assert (after["notifications"], after["incidents"]) == (before["notifications"], before["incidents"])


@pytest.mark.parametrize(
    ("rule", "error", "match"),
    [
        (BacklogRule(rule_id="backlog.lead_billing", recipient="lead_billing", scope="all_owned"),
         ValueError, "duplicate"),
        (R1.model_copy(update={"recipient": "lead_x"}), RoutingError, None),
        (BacklogRule(rule_id="backlog.lead_billing.2", recipient="lead_billing",
                     scope=QueueScope(queues=["tier_2"])), RoutingError, None),  # owned by lead_support
        (R1.model_copy(update={"critical_above": 20}), ValidationError, None),  # not above warning 20
    ],
    ids=["duplicate-rule-id", "unknown-recipient", "cross-owner-without-flag", "critical-not-above-warning"],
)  # fmt: skip
def test_rejected_add_leaves_engine_unchanged(rule: Rule, error: type[Exception], match: str | None) -> None:
    driver = Driver()
    driver.send(state_change("09:10", "a_11", "on_call"))
    before = engine_snapshot(driver.engine)
    with pytest.raises(error, match=match):
        driver.engine.add_rule(rule)
    assert engine_snapshot(driver.engine) == before


def test_added_rule_can_be_edited() -> None:
    driver = Driver()
    driver.send(state_change("09:10", "a_11", "on_call"))
    driver.engine.add_rule(R3)
    assert driver.engine.pending_timers()[(R3.rule_id, "a_11")] == at("09:40")

    assert driver.engine.update_rule(R3.model_copy(update={"min_duration_sec": 3600})) == []
    assert driver.engine.rule_version(R3.rule_id) == 2
    assert driver.engine.pending_timers()[(R3.rule_id, "a_11")] == at("10:10")  # re-timed like any edit
    emitted = driver.send(snapshot("10:11", "tier_2"))
    assert brief(emitted) == [
        ("long_call.lead_support", "a_11", "opened", None, "09:55", "10:11"),
        (R3.rule_id, "a_11", "opened", None, "10:10", "10:11"),
    ]
    assert [n.rule_version for n in emitted] == [1, 2]


# ---------------------------------------------------------------------------
# E4-E10: the dataset
# ---------------------------------------------------------------------------


def test_added_queue_rule_matches_assignment_example() -> None:
    engine = run_with_rule_added_after(0, R1)
    new, others = split(engine.notifications, R1.rule_id)
    assert new == R1_NEW_ROWS
    assert others == ORACLE
    assert len(engine.notifications) == 22
    incident = engine.incidents["backlog.lead_billing.2:billing:1"]
    assert (incident.status, incident.severity, hm(incident.opened_at), hm(incident.closed_at)) == (
        "resolved", "warning", "09:36", "09:40",
    )


def test_added_queue_rule_waits_for_next_snapshot() -> None:
    # Added at 09:36 with billing already at 22: no retroactive alert; line 52 has 20 (clear).
    engine = run_with_rule_added_after(49, R1)
    assert oracle_shape(engine.notifications) == ORACLE


def test_added_duration_rule_mid_violation_uses_strict_firing() -> None:
    engine = Engine(seeded_rules())
    replay(engine, 0, 50)  # line 50 confirms a_19's violation started 09:35
    engine.add_rule(R2)
    assert engine.pending_timers()[(R2.rule_id, "a_19")] == at("09:40")
    replay(engine, 50, 10_000)
    engine.finish()
    new, others = split(engine.notifications, R2.rule_id)
    assert new == R2_NEW_ROWS
    assert others == ORACLE


def test_added_duration_rule_past_deadline_uses_add_time() -> None:
    engine = Engine(seeded_rules())
    replay(engine, 0, 55)  # clock 09:45; a_11 on a call since 09:10, so 30 minutes passed at 09:40
    engine.add_rule(R3)
    assert engine.pending_timers()[(R3.rule_id, "a_11")] == at("09:45")
    replay(engine, 55, 10_000)
    engine.finish()
    new, others = split(engine.notifications, R3.rule_id)
    assert new == R3_NEW_ROWS
    assert others == ORACLE


def test_rule_added_after_finish_schedules_nothing() -> None:
    engine = Engine(seeded_rules())
    engine.run_all(EVENTS)
    before = engine_snapshot(engine)
    assert engine.add_rule(R4) == []
    after = engine_snapshot(engine)
    for key in ("notifications", "incidents", "clock", "timers", "finished"):
        assert after[key] == before[key], key
    assert engine.finished
    assert engine.rule_version(R4.rule_id) == 1


def test_rule_added_after_finish_runs_on_next_replay() -> None:
    engine = Engine(seeded_rules())
    engine.run_all(EVENTS)
    engine.add_rule(R4)
    engine.reset()
    engine.run_all(EVENTS)
    new, others = split(engine.notifications, R4.rule_id)
    assert new == R4_NEW_ROWS
    assert others == ORACLE
    assert len(engine.notifications) == R4_TOTAL
    assert sorted((i.rule_id, i.subject_id) for i in engine.active_incidents()) == R4_ACTIVE
    assert {key: hm(due) for key, due in engine.pending_timers().items()} == {
        ("long_call.lead_support", "a_42"): "10:33",
    }


def test_disabled_added_rule_restores_oracle() -> None:
    engine = Engine(seeded_rules())
    engine.add_rule(R4)
    engine.disable_rule(R4.rule_id)
    assert engine.rule_version(R4.rule_id) == 2
    engine.run_all(EVENTS)
    assert oracle_shape(engine.notifications) == ORACLE
    assert {key: hm(due) for key, due in engine.pending_timers().items()} == {
        ("long_call.lead_support", "a_42"): "10:33",
    }
