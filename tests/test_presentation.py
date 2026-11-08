"""Phase 4 — pure presentation helpers: rule previews and the diagnostics merge.

Synthetic inputs only; no HTTP and no database.
"""

import pytest

from alerts.presentation import merge_diagnostics, rule_preview
from alerts.rules import QueueScope, SlaRule
from alerts.state import Diagnostic
from alerts.storage import JournalRow
from tests.engine_builders import edited, seeded

SLA_TAIL = "reaches 80% of its SLA target; critical above 100%; recovers at or below 75%."


def test_backlog_preview_matches_strict_comparisons() -> None:
    # rules.py: warning when waiting > warning_above, critical when > critical_above.
    assert rule_preview(seeded("backlog.lead_billing")) == (
        "Notify lead_billing when any queue they own (billing, vip) "
        "has more than 15 tickets waiting; critical above 20."
    )


def test_sla_preview_states_all_three_ratios() -> None:
    # rules.py: critical when ratio > 1.0, warning when >= 0.80, clear when <= 0.75.
    assert rule_preview(seeded("sla.lead_support")) == (
        f"Notify lead_support when the longest wait in any queue they own (tier_2) {SLA_TAIL}"
    )


@pytest.mark.parametrize(
    ("rule_id", "seconds", "expected"),
    [
        ("adherence.lead_billing", 600,
         "Notify lead_billing when an agent they own (a_05, a_07, a_19, a_31) "
         "has been out of adherence for at least 10 minutes."),
        ("long_call.lead_support", 2700,
         "Notify lead_support when an agent they own (a_11, a_23, a_42, a_88) "
         "has been on one call for at least 45 minutes."),
        ("long_call.lead_support", 60,
         "Notify lead_support when an agent they own (a_11, a_23, a_42, a_88) "
         "has been on one call for at least 1 minute."),
        ("long_call.lead_support", 90,
         "Notify lead_support when an agent they own (a_11, a_23, a_42, a_88) "
         "has been on one call for at least 90 seconds."),
    ],
    ids=["adherence-10min", "long-call-45min", "singular-minute", "not-whole-minutes"],
)  # fmt: skip
def test_duration_previews(rule_id: str, seconds: int, expected: str) -> None:
    assert rule_preview(edited(rule_id, min_duration_sec=seconds)) == expected


def test_explicit_cross_owner_preview_names_the_real_owner() -> None:
    rule = SlaRule(rule_id="watch", recipient="lead_support", scope=QueueScope(queues=["billing"]), cross_owner=True)
    assert rule_preview(rule) == (
        f"Notify lead_support when the longest wait in any listed queue (billing) {SLA_TAIL} "
        "Cross-owner: billing is owned by lead_billing."
    )


def test_disabled_preview_is_prefixed() -> None:
    preview = rule_preview(edited("backlog.lead_billing", enabled=False))
    assert preview == "Disabled: " + rule_preview(seeded("backlog.lead_billing"))


def test_merge_diagnostics_orders_by_line_then_source() -> None:
    # Within one step the outcome is decided first, then the replay state is
    # applied, then rules run; so at the same line: journal, replay, engine.
    journal = [
        JournalRow(seq=1, kind="event", line_number=3, event_id="evt_3", outcome="accepted",
                   rule_id=None, rule_version=None),
        JournalRow(seq=2, kind="rule_change", line_number=None, event_id=None, outcome=None,
                   rule_id="backlog.lead_billing", rule_version=2),
        JournalRow(seq=3, kind="event", line_number=9, event_id="evt_9", outcome="duplicate",
                   rule_id=None, rule_version=None),
        JournalRow(seq=4, kind="event", line_number=11, event_id="evt_11", outcome="stale",
                   rule_id=None, rule_version=None),
        JournalRow(seq=5, kind="finish", line_number=None, event_id=None, outcome=None,
                   rule_id=None, rule_version=None),
    ]  # fmt: skip
    replay = [
        Diagnostic("forecast_missing", "billing", 5, "volume_forecast_next_15m is null"),
        Diagnostic("duplicate_payload_differs", "evt_9", 9, "payload differs"),
    ]
    engine = [
        Diagnostic("unowned_subject", "a_99", 5, "no lead owns this subject"),
        Diagnostic("alert_contradicted_by_late_evidence", "a_11", 7, "ended before the alert"),
    ]
    merged = merge_diagnostics(journal, replay, engine)
    assert [(d["line_number"], d["kind"], d["subject_id"], d["source"]) for d in merged] == [
        (5, "forecast_missing", "billing", "replay"),
        (5, "unowned_subject", "a_99", "engine"),
        (7, "alert_contradicted_by_late_evidence", "a_11", "engine"),
        (9, "duplicate_event", "evt_9", "journal"),
        (9, "duplicate_payload_differs", "evt_9", "replay"),
        (11, "stale_event", "evt_11", "journal"),
    ]
    assert all(d["detail"] for d in merged)  # every row explains itself
