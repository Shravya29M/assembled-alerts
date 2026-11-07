"""Checkpoint 3.1 — full dataset replay against the PLAN §7.1 oracle.

ORACLE is copied verbatim from docs/PLAN.md §7.1 (hand replay, baseline
commit 987b34f). It must not be edited to match the engine: any mismatch is
investigated row by row and fixed only with approval (Decision 51).

Resolved notifications carry the incident's last severity.
"""

from pathlib import Path

import pytest

from alerts.engine import Engine
from alerts.ingest import SourcedEvent, load_events
from alerts.routing import seeded_rules
from tests.builders import at
from tests.engine_builders import hm

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"
EVENTS: list[SourcedEvent] = load_events(DATA_PATH).accepted

# (emitted_at, effective_at, rule_id, subject, kind, severity, recipient)
ORACLE = [
    ("09:30", "09:30", "backlog.lead_billing", "billing", "opened", "warning", "lead_billing"),
    ("09:30", "09:30", "sla.lead_billing", "billing", "opened", "critical", "lead_billing"),
    ("09:36", "09:36", "backlog.lead_billing", "billing", "escalated", "critical", "lead_billing"),
    ("09:45", "09:45", "sla.lead_support", "tier_2", "opened", "warning", "lead_support"),
    ("09:48", "09:45", "adherence.lead_billing", "a_19", "opened", None, "lead_billing"),
    ("10:00", "09:55", "long_call.lead_support", "a_11", "opened", None, "lead_support"),
    ("10:00", "10:00", "backlog.lead_billing", "billing", "resolved", "warning", "lead_billing"),
    ("10:00", "10:00", "sla.lead_support", "tier_2", "escalated", "critical", "lead_support"),
    ("10:10:30", "10:10", "long_call.lead_billing", "a_07", "opened", None, "lead_billing"),
    ("10:10:30", "10:10", "adherence.lead_support", "a_88", "opened", None, "lead_support"),
    ("10:10:30", "10:10:30", "adherence.lead_billing", "a_19", "resolved", None, "lead_billing"),
    ("10:15", "10:15", "sla.lead_billing", "billing", "resolved", "critical", "lead_billing"),
    ("10:15", "10:15", "sla.lead_support", "tier_2", "resolved", "critical", "lead_support"),
    ("10:15:30", "10:15", "long_call.lead_billing", "a_31", "opened", None, "lead_billing"),
    ("10:16", "10:16", "sla.lead_billing", "billing", "opened", "warning", "lead_billing"),
    ("10:20", "10:20", "long_call.lead_support", "a_11", "resolved", None, "lead_support"),
    ("10:25", "10:25", "long_call.lead_billing", "a_31", "resolved", None, "lead_billing"),
    ("10:30", "10:25:30", "adherence.lead_support", "a_23", "opened", None, "lead_support"),
    ("10:30", "10:30", "sla.lead_billing", "billing", "resolved", "warning", "lead_billing"),
    ("10:30", "10:30", "long_call.lead_support", "a_23", "opened", None, "lead_support"),
]  # fmt: skip


@pytest.fixture(scope="module")
def engine() -> Engine:
    engine = Engine(seeded_rules())
    engine.run_all(EVENTS)
    return engine


def test_matches_oracle_exactly(engine: Engine) -> None:
    actual = [
        (hm(n.emitted_at), hm(n.effective_at), n.rule_id, n.subject_id, n.kind, n.severity, n.recipient)
        for n in engine.notifications
    ]
    assert actual == ORACLE


def test_exactly_four_incidents_active_at_end(engine: Engine) -> None:
    assert {(i.rule_id, i.subject_id) for i in engine.active_incidents()} == {
        ("long_call.lead_billing", "a_07"),
        ("long_call.lead_support", "a_23"),
        ("adherence.lead_support", "a_23"),
        ("adherence.lead_support", "a_88"),
    }


def test_billing_backlog_downgrade_at_0940_is_silent() -> None:
    engine = Engine(seeded_rules())
    for sourced in EVENTS:
        result = engine.step(sourced)
        if sourced.line_number == 52:  # billing 20 waiting: critical -> warning
            assert result.notifications == []
            break
    backlog = [i for i in engine.active_incidents() if i.rule_id == "backlog.lead_billing"]
    assert [i.severity for i in backlog] == ["warning"]


def test_a19_resolves_on_line_76_not_line_75(engine: Engine) -> None:
    resolved = [n for n in engine.notifications if n.subject_id == "a_19" and n.kind == "resolved"]
    assert [(n.line_number, hm(n.emitted_at)) for n in resolved] == [(76, "10:10:30")]


def test_negative_expectations(engine: Engine) -> None:
    subjects = [n.subject_id for n in engine.notifications]
    assert "a_42" not in subjects  # deadline 10:33 is beyond the horizon
    assert "a_05" not in subjects
    assert subjects.count("a_07") == 1  # first call (09:05:30-09:18) never fires
    assert not [n for n in engine.notifications if n.line_number in (95, 96)]
    assert engine.pending_timers()[("long_call.lead_support", "a_42")] == at("10:33")


def test_billing_sla_has_two_distinct_incidents(engine: Engine) -> None:
    sla = [i for i in engine.incidents.values() if i.rule_id == "sla.lead_billing"]
    assert [(i.incident_id, i.status, hm(i.opened_at), hm(i.closed_at)) for i in sla] == [
        ("sla.lead_billing:billing:1", "resolved", "09:30", "10:15"),
        ("sla.lead_billing:billing:2", "resolved", "10:16", "10:30"),
    ]


def test_reset_and_rerun_is_identical(engine: Engine) -> None:
    first = list(engine.notifications)
    rerun = Engine(seeded_rules())
    rerun.run_all(EVENTS)
    rerun.reset()
    rerun.run_all(EVENTS)
    assert rerun.notifications == first
