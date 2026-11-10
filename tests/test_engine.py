"""Checkpoint 3.1 — engine timer boundaries, ordering, cancellation, late events.

Synthetic events with the eight seeded rules. a_11 and a_88 belong to
lead_support; a_07, a_19 and billing belong to lead_billing.
"""

import pytest

from alerts.engine import Engine, ReplayFinishedError
from alerts.ingest import SourcedEvent
from alerts.routing import seeded_rules
from tests.builders import adherence, at, snapshot, state_change
from tests.engine_builders import Driver, brief, edited, engine_snapshot, seeded

LONG_A11 = "long_call.lead_support"


def a11_on_call(ts: str = "09:10") -> object:
    return state_change(ts, "a_11", "on_call")


def a11_exit(ts: str) -> object:
    return state_change(ts, "a_11", "available", previous_state="on_call")


def quiet(ts: str) -> object:
    """A tier_2 snapshot that triggers no queue rule; it only moves the clock."""
    return snapshot(ts, "tier_2")


# ---------------------------------------------------------------------------
# Strict deadline boundary (Decision 43)
# ---------------------------------------------------------------------------


def test_event_before_deadline_does_not_fire() -> None:
    driver = Driver()
    driver.send(a11_on_call())
    assert driver.send(quiet("09:54")) == []
    assert driver.engine.pending_timers() == {(LONG_A11, "a_11"): at("09:55")}


def test_event_exactly_at_deadline_does_not_fire() -> None:
    driver = Driver()
    driver.send(a11_on_call())
    assert driver.send(quiet("09:55")) == []


def test_event_after_deadline_fires_before_event_is_applied() -> None:
    driver = Driver()
    driver.send(a11_on_call())
    emitted = driver.send(snapshot("09:56", "billing", tickets_waiting=18))
    assert brief(emitted) == [
        (LONG_A11, "a_11", "opened", None, "09:55", "09:56"),
        ("backlog.lead_billing", "billing", "opened", "warning", "09:56", "09:56"),
    ]
    assert emitted[0].line_number == 2  # the event whose arrival fired the timer


@pytest.mark.parametrize("exit_first", [True, False], ids=["exit-first", "exit-last"])
def test_same_timestamp_exit_cancels_wherever_it_sits(exit_first: bool) -> None:
    driver = Driver()
    driver.send(a11_on_call())
    at_deadline = [a11_exit("09:55"), quiet("09:55")]
    driver.send(*(at_deadline if exit_first else reversed(at_deadline)))
    assert driver.send(quiet("10:00")) == []
    assert driver.engine.notifications == []


def test_exit_as_first_event_after_deadline_opens_then_resolves() -> None:
    driver = Driver()
    driver.send(a11_on_call())
    assert brief(driver.send(a11_exit("09:56"))) == [
        (LONG_A11, "a_11", "opened", None, "09:55", "09:56"),
        (LONG_A11, "a_11", "resolved", None, "09:56", "09:56"),
    ]


def test_end_of_input_fires_deadline_at_horizon() -> None:
    driver = Driver()
    driver.send(a11_on_call(), quiet("09:55"))
    emitted = driver.finish()
    assert brief(emitted) == [(LONG_A11, "a_11", "opened", None, "09:55", "09:55")]
    assert emitted[0].line_number is None


def test_end_of_input_never_fires_beyond_horizon() -> None:
    driver = Driver()
    driver.send(a11_on_call(), quiet("09:54"))
    assert driver.finish() == []
    assert driver.engine.pending_timers() == {(LONG_A11, "a_11"): at("09:55")}


def test_duplicate_with_later_ts_does_not_fire() -> None:
    driver = Driver()
    on_call = state_change("09:10", "a_11", "on_call", event_id="evt_a")
    driver.send(on_call, quiet("09:50"))
    duplicate = state_change("10:00", "a_11", "on_call", event_id="evt_a")
    assert driver.send(duplicate) == []
    assert driver.engine.state.clock == at("09:50")
    assert driver.engine.pending_timers() == {(LONG_A11, "a_11"): at("09:55")}


def test_stale_snapshot_that_would_alert_is_ignored_by_the_engine() -> None:
    # Control: on its own, this snapshot opens a critical backlog alert (30 > 20).
    would_alert = snapshot("09:50", "billing", tickets_waiting=30)
    control = Driver()
    assert brief(control.send(would_alert)) == [
        ("backlog.lead_billing", "billing", "opened", "critical", "09:50", "09:50"),
    ]

    # After a newer billing snapshot, the same evidence is stale and changes nothing.
    engine = Engine(seeded_rules())
    newer = engine.step(SourcedEvent(1, snapshot("10:00", "billing", tickets_waiting=3)))
    stale = engine.step(SourcedEvent(2, snapshot("09:50", "billing", tickets_waiting=30)))
    assert (newer.outcome, stale.outcome) == ("accepted", "stale")
    assert stale.notifications == []
    assert engine.notifications == []
    assert engine.incidents == {}
    assert engine.state.clock == at("10:00")
    assert engine.state.queues["billing"].tickets_waiting == 3


def a11_offline(ts: str) -> object:
    return state_change(ts, "a_11", "offline", previous_state="on_call")


def test_exit_to_offline_before_deadline_cancels_timer() -> None:
    driver = Driver()
    driver.send(a11_on_call())
    assert driver.send(a11_offline("09:30")) == []
    assert driver.engine.pending_timers() == {}
    assert driver.send(quiet("10:00")) == []  # past the old 09:55 deadline
    assert driver.engine.notifications == []


def test_exit_to_offline_after_alert_resolves_incident() -> None:
    driver = Driver()
    driver.send(a11_on_call())
    assert brief(driver.send(quiet("09:56"))) == [(LONG_A11, "a_11", "opened", None, "09:55", "09:56")]
    assert brief(driver.send(a11_offline("10:05"))) == [
        (LONG_A11, "a_11", "resolved", None, "10:05", "10:05"),
    ]
    assert {key: incident.status for key, incident in driver.engine.incidents.items()} == {
        "long_call.lead_support:a_11:1": "resolved",
    }


def test_offline_then_on_call_starts_new_episode() -> None:
    driver = Driver()
    driver.send(a11_on_call(), a11_offline("09:30"))
    driver.send(state_change("10:00", "a_11", "on_call", previous_state="offline"))
    assert driver.engine.pending_timers() == {(LONG_A11, "a_11"): at("10:45")}
    assert brief(driver.send(quiet("10:46"))) == [(LONG_A11, "a_11", "opened", None, "10:45", "10:46")]


def test_adherence_clear_cancels_timer() -> None:
    driver = Driver()
    driver.send(
        adherence("09:36", "a_19", in_violation=True, started="09:35"),
        adherence("09:40", "a_19", in_violation=False, actual_state="available"),
    )
    assert driver.send(quiet("10:00")) == []
    assert driver.engine.pending_timers() == {}


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("a07_first", [True, False], ids=["a07-created-first", "a88-created-first"])
def test_equal_deadlines_fire_in_creation_order(a07_first: bool) -> None:
    a07 = state_change("09:25", "a_07", "on_call")
    a88 = adherence("10:01", "a_88", in_violation=True, started="10:00")
    driver = Driver()
    driver.send(*([a07, a88] if a07_first else [a88, a07]))
    order = [n.subject_id for n in driver.send(quiet("10:10:30"))]
    assert order == (["a_07", "a_88"] if a07_first else ["a_88", "a_07"])


def test_rules_within_one_event_run_in_rule_id_order() -> None:
    driver = Driver()
    emitted = driver.send(snapshot("09:30", "billing", tickets_waiting=18, longest_wait_sec=130))
    assert brief(emitted) == [
        ("backlog.lead_billing", "billing", "opened", "warning", "09:30", "09:30"),
        ("sla.lead_billing", "billing", "opened", "critical", "09:30", "09:30"),
    ]


def test_notification_seq_is_contiguous_in_emitted_order() -> None:
    driver = Driver()
    driver.send(a11_on_call(), snapshot("09:56", "billing", tickets_waiting=22, longest_wait_sec=130))
    assert [n.seq for n in driver.engine.notifications] == [1, 2, 3]


def test_incident_ids_count_per_rule_and_subject() -> None:
    driver = Driver()
    driver.send(
        snapshot("09:30", "billing", tickets_waiting=18),
        snapshot("09:40", "billing", tickets_waiting=10),
        snapshot("09:50", "billing", tickets_waiting=18),
    )
    ids = [n.incident_id for n in driver.engine.notifications]
    assert ids == ["backlog.lead_billing:billing:1"] * 2 + ["backlog.lead_billing:billing:2"]


def test_same_inputs_give_identical_runs() -> None:
    events = [a11_on_call(), snapshot("09:30", "billing", tickets_waiting=18), quiet("10:00")]
    first, second = Driver(), Driver()
    first.send(*events)
    second.send(*events)
    assert first.engine.notifications == second.engine.notifications
    assert first.engine.incidents == second.engine.incidents


# ---------------------------------------------------------------------------
# Late-but-fresh events (Decisions 23, 35, 43, 44)
# ---------------------------------------------------------------------------


def test_late_on_call_with_past_deadline_fires_right_after_the_event() -> None:
    driver = Driver()
    driver.send(quiet("10:30"))
    emitted = driver.send(a11_on_call("09:40"))  # fresh for a_11, behind the clock
    assert brief(emitted) == [(LONG_A11, "a_11", "opened", None, "10:25", "10:30")]


def test_late_snapshot_effective_at_event_ts_emitted_at_clock() -> None:
    driver = Driver()
    driver.send(snapshot("10:00", "billing"))
    emitted = driver.send(snapshot("09:50", "tier_2", longest_wait_sec=320, sla_target_sec=300))
    assert brief(emitted) == [("sla.lead_support", "tier_2", "opened", "critical", "09:50", "10:00")]


def test_late_exit_before_deadline_cancels_pending_timer() -> None:
    driver = Driver()
    driver.send(a11_on_call(), quiet("09:50"))
    assert driver.send(a11_exit("09:40")) == []  # late but fresh; call lasted 30 min
    assert driver.engine.pending_timers() == {}
    assert driver.send(quiet("10:00")) == []


def test_late_exit_after_alert_resolves_at_event_ts() -> None:
    driver = Driver()
    driver.send(a11_on_call())
    driver.send(quiet("10:05"))  # fires the 09:55 timer
    assert brief(driver.send(a11_exit("10:00"))) == [
        (LONG_A11, "a_11", "resolved", None, "10:00", "10:05"),
    ]


def test_late_exit_before_deadline_after_alert_already_fired() -> None:
    # The call really ended at 09:50, before the 09:55 deadline, but that
    # evidence arrived only after the alert had fired. Under the agreed
    # semantics the resolution is effective at the exit's own ts.
    driver = Driver()
    driver.send(a11_on_call())
    driver.send(quiet("10:05"))
    assert brief(driver.send(a11_exit("09:50"))) == [
        (LONG_A11, "a_11", "resolved", None, "09:50", "10:05"),
    ]


@pytest.mark.parametrize(
    ("exit_ts", "contradicted"),
    [("09:50", True), ("09:55", False), ("10:00", False)],
    ids=["before-deadline", "at-deadline", "after-deadline"],
)
def test_late_exit_before_deadline_records_contradiction(exit_ts: str, contradicted: bool) -> None:
    # The alert opened at 09:55. A late exit before 09:55 proves the call
    # never reached 45 minutes; at or after 09:55 the alert was valid
    # (duration conditions are inclusive). History is never rewritten.
    driver = Driver()
    driver.send(a11_on_call())
    driver.send(quiet("10:05"))
    driver.send(a11_exit(exit_ts))
    assert [n.kind for n in driver.engine.notifications] == ["opened", "resolved"]
    expected = [("alert_contradicted_by_late_evidence", "a_11", 3)] if contradicted else []
    assert [(d.kind, d.subject_id, d.line_number) for d in driver.engine.diagnostics] == expected


# ---------------------------------------------------------------------------
# Checkpoint 3.2 — finalization boundary (Decision 62) and construction
# ---------------------------------------------------------------------------


def test_step_after_finish_is_rejected_and_changes_nothing() -> None:
    driver = Driver()
    driver.send(a11_on_call(), quiet("09:55"))
    driver.finish()
    before = engine_snapshot(driver.engine)
    with pytest.raises(ReplayFinishedError):
        driver.send(a11_exit("09:55"))  # would otherwise resolve the alert fired at the horizon
    assert engine_snapshot(driver.engine) == before


def test_repeated_finish_returns_nothing_new() -> None:
    driver = Driver()
    driver.send(a11_on_call(), quiet("09:55"))
    assert len(driver.finish()) == 1
    assert driver.finish() == []
    assert len(driver.engine.notifications) == 1


def test_reset_allows_replay_again_with_current_rules() -> None:
    driver = Driver()
    driver.send(a11_on_call(), quiet("09:55"))
    driver.finish()
    driver.engine.update_rule(edited(LONG_A11, min_duration_sec=1800))
    driver.engine.reset()
    driver.line = 0
    emitted = driver.send(a11_on_call(), quiet("09:41"))  # 30-minute rule: due 09:40
    assert brief(emitted) == [(LONG_A11, "a_11", "opened", None, "09:40", "09:41")]
    assert emitted[0].rule_version == 2


def test_duplicate_rule_ids_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate rule_id"):
        Engine([*seeded_rules(), seeded(LONG_A11)])
