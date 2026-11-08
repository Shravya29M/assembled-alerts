"""Checkpoint 3.2 — disable, re-enable and edit rules (Decisions 45-47).

Edits and enable/disable never emit notifications themselves and never
rewrite history. Every rule change increments the rule's version.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from alerts.engine import Engine
from alerts.ingest import load_events
from alerts.routing import RoutingError, seeded_rules
from alerts.rules import BacklogRule, LongCallRule, Rule
from tests.builders import at, snapshot, state_change
from tests.engine_builders import Driver, brief, edited, engine_snapshot, seeded

LONG_A11 = "long_call.lead_support"
BACKLOG = "backlog.lead_billing"
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"


def quiet(ts: str) -> object:
    return snapshot(ts, "tier_2")


def billing(ts: str, waiting: int) -> object:
    return snapshot(ts, "billing", tickets_waiting=waiting)


# ---------------------------------------------------------------------------
# Disable / re-enable
# ---------------------------------------------------------------------------


def test_disable_closes_active_incident_without_notification() -> None:
    driver = Driver()
    driver.send(billing("09:30", 18))
    assert driver.engine.disable_rule(BACKLOG) == []
    incident = driver.engine.incidents[f"{BACKLOG}:billing:1"]
    assert (incident.status, incident.closed_at) == ("disabled", at("09:30"))
    assert len(driver.engine.notifications) == 1  # only the original 'opened'


def test_disable_cancels_pending_timer() -> None:
    driver = Driver()
    driver.send(state_change("09:10", "a_11", "on_call"), quiet("09:50"))
    driver.engine.disable_rule(LONG_A11)
    assert driver.engine.pending_timers() == {}
    assert driver.send(quiet("10:00")) == []


def test_disabled_rule_ignores_later_events() -> None:
    driver = Driver()
    driver.engine.disable_rule(BACKLOG)
    assert driver.send(billing("09:30", 22)) == []
    assert not [i for i in driver.engine.incidents.values() if i.rule_id == BACKLOG]


def test_reenabled_queue_rule_waits_for_next_snapshot() -> None:
    driver = Driver()
    driver.send(billing("09:30", 18))
    driver.engine.disable_rule(BACKLOG)
    assert driver.engine.enable_rule(BACKLOG) == []
    assert brief(driver.send(billing("09:36", 18))) == [(BACKLOG, "billing", "opened", "warning", "09:36", "09:36")]
    assert driver.engine.notifications[-1].incident_id == f"{BACKLOG}:billing:2"


def test_reenabled_duration_rule_uses_enable_time_and_strict_firing() -> None:
    driver = Driver()
    driver.send(state_change("09:10", "a_11", "on_call"), quiet("09:20"))
    driver.engine.disable_rule(LONG_A11)
    driver.send(quiet("10:00"))
    assert driver.engine.enable_rule(LONG_A11) == []
    assert driver.send(quiet("10:00")) == []  # same ts as the enable: not yet
    assert brief(driver.send(quiet("10:01"))) == [(LONG_A11, "a_11", "opened", None, "10:00", "10:01")]


# ---------------------------------------------------------------------------
# Duration edits at the current clock
# ---------------------------------------------------------------------------


def lowered_at_0950(on_call_since: str) -> Driver:
    """a_11 on a call; clock 09:50; long-call threshold lowered to 30 min."""
    driver = Driver()
    driver.send(state_change(on_call_since, "a_11", "on_call"), quiet("09:50"))
    assert driver.engine.update_rule(edited(LONG_A11, min_duration_sec=1800)) == []
    return driver


def test_lowered_deadline_equal_to_edit_time_waits_for_later_event() -> None:
    driver = lowered_at_0950("09:20")  # new deadline 09:50 == edit time
    assert driver.engine.pending_timers()[(LONG_A11, "a_11")] == at("09:50")
    assert driver.send(quiet("09:50")) == []  # strict boundary
    assert brief(driver.send(quiet("09:51"))) == [(LONG_A11, "a_11", "opened", None, "09:50", "09:51")]


def test_lowered_deadline_equal_to_edit_time_cancelled_by_same_ts_exit() -> None:
    driver = lowered_at_0950("09:20")
    driver.send(state_change("09:50", "a_11", "available", previous_state="on_call"))
    assert driver.send(quiet("09:51")) == []


def test_lowered_deadline_equal_to_edit_time_fires_at_inclusive_horizon() -> None:
    driver = lowered_at_0950("09:20")
    emitted = driver.finish()
    assert brief(emitted) == [(LONG_A11, "a_11", "opened", None, "09:50", "09:50")]
    assert emitted[0].line_number is None


def test_lowered_deadline_already_past_uses_edit_time() -> None:
    driver = lowered_at_0950("09:10")  # new deadline 09:40 < edit time 09:50
    assert driver.send(quiet("09:50")) == []
    assert brief(driver.send(quiet("09:51"))) == [(LONG_A11, "a_11", "opened", None, "09:50", "09:51")]


def test_raised_threshold_replaces_pending_timer() -> None:
    driver = Driver()
    driver.send(state_change("09:10", "a_11", "on_call"), quiet("09:50"))
    assert driver.engine.update_rule(edited(LONG_A11, min_duration_sec=3600)) == []
    assert driver.engine.pending_timers() == {(LONG_A11, "a_11"): at("10:10")}
    assert driver.send(quiet("09:56")) == []
    assert brief(driver.send(quiet("10:11"))) == [(LONG_A11, "a_11", "opened", None, "10:10", "10:11")]


def test_raised_threshold_keeps_active_duration_incident() -> None:
    driver = Driver()
    driver.send(state_change("09:10", "a_11", "on_call"), quiet("10:00"))  # opens (09:55)
    assert driver.engine.update_rule(edited(LONG_A11, min_duration_sec=5400)) == []
    assert [i.subject_id for i in driver.engine.active_incidents()] == ["a_11"]
    resolved = driver.send(state_change("10:20", "a_11", "available", previous_state="on_call"))
    assert [n.kind for n in resolved] == ["resolved"]
    assert len(driver.engine.notifications) == 2


# ---------------------------------------------------------------------------
# Queue-rule edits apply at the next snapshot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("next_waiting", "expected_kind", "expected_severity"),
    [(18, "resolved", "warning"), (22, "escalated", "critical")],
)
def test_backlog_edit_applies_at_next_snapshot(next_waiting: int, expected_kind: str, expected_severity: str) -> None:
    driver = Driver()
    driver.send(billing("09:30", 18))
    assert driver.engine.update_rule(edited(BACKLOG, warning_above=18)) == []
    assert [i.severity for i in driver.engine.active_incidents()] == ["warning"]
    emitted = driver.send(billing("09:36", next_waiting))
    assert [(n.kind, n.severity) for n in emitted] == [(expected_kind, expected_severity)]


def test_notifications_record_rule_version() -> None:
    driver = Driver()
    first = driver.send(billing("09:30", 18))
    driver.engine.update_rule(edited(BACKLOG, warning_above=18))
    assert driver.engine.rule_version(BACKLOG) == 2
    second = driver.send(billing("09:36", 22))
    assert [n.rule_version for n in first + second] == [1, 2]


# ---------------------------------------------------------------------------
# Invalid edits and immutable history
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "replacement",
    [
        lambda: LongCallRule(rule_id=LONG_A11, recipient="lead_support", scope={"agents": ["a_19"]}),
        lambda: edited(LONG_A11, recipient="lead_x"),
    ],
    ids=["cross-owner-without-flag", "unknown-recipient"],
)
def test_invalid_edit_rejected_and_old_rule_kept(replacement: object) -> None:
    engine = Engine(seeded_rules())
    with pytest.raises(RoutingError):
        engine.update_rule(replacement())
    assert engine.rules[LONG_A11] == seeded(LONG_A11)
    assert engine.rule_version(LONG_A11) == 1


def test_rule_changes_never_rewrite_history() -> None:
    engine = Engine(seeded_rules())
    events = load_events(DATA_PATH).accepted
    for sourced in events[:60]:
        engine.step(sourced)
    before = list(engine.notifications)
    engine.disable_rule("sla.lead_billing")
    engine.update_rule(edited(LONG_A11, min_duration_sec=3600))
    for sourced in events[60:70]:
        engine.step(sourced)
    assert engine.notifications[: len(before)] == before


# ---------------------------------------------------------------------------
# Recipient edits while an incident is ACTIVE (regression: one active incident
# per rule x subject, whatever the recipient)
# ---------------------------------------------------------------------------


def watch_rule(rule_class: type, scope: dict[str, list[str]], recipient: str, **fields: object) -> Rule:
    return rule_class(rule_id="watch", recipient=recipient, scope=scope, **fields)


def watch_incidents(engine: Engine) -> list[tuple[str, str, str]]:
    return [(i.incident_id, i.status, i.recipient) for i in engine.incidents.values() if i.rule_id == "watch"]


def watch_notifications(engine: Engine) -> list[tuple[str, str, str]]:
    return [(n.incident_id, n.kind, n.recipient) for n in engine.notifications if n.rule_id == "watch"]


def test_recipient_edit_keeps_one_active_queue_incident() -> None:
    old = watch_rule(BacklogRule, {"queues": ["billing"]}, "lead_billing")
    driver = Driver([*seeded_rules(), old])
    driver.send(billing("09:30", 18))
    history = list(driver.engine.notifications)

    new = watch_rule(BacklogRule, {"queues": ["billing"]}, "lead_support", cross_owner=True)
    assert driver.engine.update_rule(new) == []  # the edit itself is silent
    assert watch_incidents(driver.engine) == [("watch:billing:1", "active", "lead_support")]

    driver.send(billing("09:36", 22))
    assert watch_incidents(driver.engine) == [("watch:billing:1", "active", "lead_support")]
    assert watch_notifications(driver.engine) == [
        ("watch:billing:1", "opened", "lead_billing"),  # history unchanged
        ("watch:billing:1", "escalated", "lead_support"),  # future changes go to the new recipient
    ]
    assert driver.engine.notifications[: len(history)] == history


def test_recipient_edit_keeps_one_active_duration_incident() -> None:
    old = watch_rule(LongCallRule, {"agents": ["a_11"]}, "lead_support")
    driver = Driver([*seeded_rules(), old])
    driver.send(state_change("09:10", "a_11", "on_call"), quiet("09:56"))  # alert fires

    new = watch_rule(LongCallRule, {"agents": ["a_11"]}, "lead_billing", cross_owner=True)
    assert driver.engine.update_rule(new) == []
    # A repeated on_call is still 'met'; it must not schedule a second alert.
    driver.send(state_change("10:00", "a_11", "on_call", previous_state="on_call"))
    driver.send(state_change("10:05", "a_11", "available", previous_state="on_call"))
    assert watch_incidents(driver.engine) == [("watch:a_11:1", "resolved", "lead_billing")]
    assert watch_notifications(driver.engine) == [
        ("watch:a_11:1", "opened", "lead_support"),
        ("watch:a_11:1", "resolved", "lead_billing"),
    ]


# ---------------------------------------------------------------------------
# Scope edits: a subject that leaves the scope is closed as disabled, not recovered
# ---------------------------------------------------------------------------


def test_scope_removal_closes_active_incident_as_disabled() -> None:
    old = watch_rule(BacklogRule, {"queues": ["billing", "vip"]}, "lead_billing")
    driver = Driver([*seeded_rules(), old])
    driver.send(billing("09:30", 18), quiet("09:40"))
    history = list(driver.engine.notifications)

    assert driver.engine.update_rule(watch_rule(BacklogRule, {"queues": ["vip"]}, "lead_billing")) == []
    incident = driver.engine.incidents["watch:billing:1"]
    assert (incident.status, incident.closed_at) == ("disabled", at("09:40"))  # the clock, not the last snapshot
    assert driver.engine.notifications == history
    driver.send(billing("09:45", 22))  # watch no longer covers billing: no new watch incident
    assert watch_incidents(driver.engine) == [("watch:billing:1", "disabled", "lead_billing")]
    assert watch_notifications(driver.engine) == [("watch:billing:1", "opened", "lead_billing")]


def test_scope_removal_cancels_pending_timer() -> None:
    old = watch_rule(LongCallRule, {"agents": ["a_11", "a_88"]}, "lead_support")
    driver = Driver([*seeded_rules(), old])
    driver.send(state_change("09:10", "a_11", "on_call"), quiet("09:50"))
    driver.engine.update_rule(watch_rule(LongCallRule, {"agents": ["a_88"]}, "lead_support"))
    assert ("watch", "a_11") not in driver.engine.pending_timers()
    driver.send(quiet("10:00"))
    assert watch_notifications(driver.engine) == []


def test_all_owned_recipient_change_swaps_monitored_subjects() -> None:
    driver = Driver()
    driver.send(
        state_change("09:10", "a_11", "on_call"),  # lead_support's agent
        state_change("09:30", "a_31", "on_call"),  # lead_billing's agent
        quiet("10:00"),  # a_11 alert fires (09:55)
    )
    assert driver.engine.update_rule(edited(LONG_A11, recipient="lead_billing")) == []

    a11 = driver.engine.incidents[f"{LONG_A11}:a_11:1"]
    assert (a11.status, a11.closed_at) == ("disabled", at("10:00"))  # a_11 left the scope
    timers = driver.engine.pending_timers()
    assert (LONG_A11, "a_11") not in timers
    assert timers[(LONG_A11, "a_31")] == at("10:15")  # newly covered: recomputed from current state

    emitted = driver.send(quiet("10:16"))
    assert {(n.rule_id, n.subject_id, n.recipient) for n in emitted} == {
        ("long_call.lead_billing", "a_31", "lead_billing"),
        (LONG_A11, "a_31", "lead_billing"),
    }
    assert len(driver.engine.notifications) == 3  # no notification from the edit itself


# ---------------------------------------------------------------------------
# Edits after finish(): configuration changes, but nothing new is created
# ---------------------------------------------------------------------------


def test_edit_after_finish_creates_no_timers_incidents_or_notifications() -> None:
    driver = Driver()
    driver.send(state_change("09:10", "a_11", "on_call"), quiet("09:50"))
    assert driver.finish() == []  # deadline 09:55 is beyond the 09:50 horizon
    before = engine_snapshot(driver.engine)

    assert driver.engine.update_rule(edited(LONG_A11, min_duration_sec=1800)) == []  # would be due 09:40
    after = engine_snapshot(driver.engine)
    assert after["rules"][LONG_A11].min_duration_sec == 1800
    assert after["versions"][LONG_A11] == 2
    assert after["timers"] == []  # due 09:40 <= horizon: cancelled, never fired (Decision 68)
    unchanged = ("rules", "versions", "timers")
    assert {k: v for k, v in after.items() if k not in unchanged} == {
        k: v for k, v in before.items() if k not in unchanged
    }
    assert driver.finish() == []


def test_disable_after_finish_closes_incident_silently() -> None:
    driver = Driver()
    driver.send(billing("09:30", 18))
    driver.finish()
    assert driver.engine.disable_rule(BACKLOG) == []
    assert driver.engine.incidents[f"{BACKLOG}:billing:1"].status == "disabled"
    assert len(driver.engine.notifications) == 1


# ---------------------------------------------------------------------------
# Every rejected edit leaves the whole engine unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("replacement", "error"),
    [
        (lambda: edited(BACKLOG, recipient="lead_x"), RoutingError),
        # model_copy skips Pydantic validation; the engine must re-validate.
        (lambda: seeded(BACKLOG).model_copy(update={"warning_above": 25}), ValidationError),
        (lambda: seeded(LONG_A11).model_copy(update={"min_duration_sec": 0}), ValidationError),
        (lambda: LongCallRule(rule_id="no_such_rule", recipient="lead_support", scope="all_owned"), ValueError),
    ],
    ids=["bad-routing", "warning-above-critical", "zero-duration", "unknown-rule-id"],
)
def test_invalid_edit_leaves_engine_state_unchanged(replacement: object, error: type[Exception]) -> None:
    driver = Driver()
    driver.send(billing("09:30", 18), state_change("09:31", "a_11", "on_call"))  # incident + timer
    before = engine_snapshot(driver.engine)
    with pytest.raises(error):
        driver.engine.update_rule(replacement())
    assert engine_snapshot(driver.engine) == before


def test_disable_after_finish_cancels_pending_timer_silently() -> None:
    driver = Driver()
    driver.send(state_change("09:10", "a_11", "on_call"), quiet("09:50"))
    driver.finish()  # 09:55 deadline is beyond the horizon: still pending
    assert driver.engine.pending_timers() == {(LONG_A11, "a_11"): at("09:55")}
    assert driver.engine.disable_rule(LONG_A11) == []
    assert driver.engine.pending_timers() == {}
    assert driver.engine.notifications == []
    assert driver.finish() == []


@pytest.mark.parametrize(
    ("minutes", "expected_deadline"),
    [(30, None), (40, None), (41, "09:51"), (60, "10:10")],
    ids=["lowered-past-horizon", "exactly-at-horizon", "just-after-horizon", "raised"],
)
def test_edit_after_finish_moves_or_cancels_existing_timer(minutes: int, expected_deadline: str | None) -> None:
    # a_11 on a call since 09:10; the replay ends at 09:50 with the 09:55 timer pending.
    driver = Driver()
    driver.send(state_change("09:10", "a_11", "on_call"), quiet("09:50"))
    driver.finish()
    driver.engine.update_rule(edited(LONG_A11, min_duration_sec=minutes * 60))
    expected = {} if expected_deadline is None else {(LONG_A11, "a_11"): at(expected_deadline)}
    assert driver.engine.pending_timers() == expected
    assert driver.finish() == []  # still finalized: a moved timer never fires
    assert driver.engine.notifications == [] and driver.engine.incidents == {}


def test_edit_after_finish_adds_no_timer_for_newly_covered_subject() -> None:
    driver = Driver()
    driver.send(
        state_change("09:30", "a_31", "on_call"),  # lead_billing's agent
        state_change("09:40", "a_11", "on_call"),  # lead_support's agent, due 10:25
        quiet("10:00"),
    )
    driver.finish()
    driver.engine.update_rule(edited(LONG_A11, recipient="lead_billing"))  # all_owned: swaps subjects
    timers = driver.engine.pending_timers()
    assert (LONG_A11, "a_11") not in timers  # left the scope: cancelled
    assert (LONG_A11, "a_31") not in timers  # newly covered: no new timer after finish
    assert timers == {("long_call.lead_billing", "a_31"): at("10:15")}  # other rules untouched
