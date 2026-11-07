"""Phase 2 rule evaluation: config validation and pure threshold decisions.

Synthetic state only. evaluate(rule, subject_state, clock) must return a
Decision without knowing anything about incidents.

Rules are always built inside test bodies (via make), never in parametrize
lists, so a broken constructor fails one test instead of breaking collection.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from alerts.rules import (
    AdherenceRule,
    BacklogRule,
    Decision,
    LongCallRule,
    SlaRule,
    evaluate,
)
from alerts.state import AdherenceState, CallState
from tests.builders import at, snapshot

RULE_CLASSES = [BacklogRule, SlaRule, AdherenceRule, LongCallRule]


def make(rule_class: type, **overrides: Any) -> Any:
    """A valid rule of rule_class, with explicit scope, plus overrides."""
    return rule_class(**{"rule_id": "r", "scope": "all_owned", "recipient": "lead_billing", **overrides})


def on_call_since(since: str | None) -> CallState:
    return CallState(
        agent_id="a_11",
        state="on_call" if since is not None else "available",
        since=at(since or "09:00"),
        on_call_since=at(since) if since is not None else None,
    )


def violation_since(start: str | None) -> AdherenceState:
    return AdherenceState(
        agent_id="a_19",
        in_violation=start is not None,
        effective_start=at(start) if start is not None else None,
        actual_state="on_break" if start is not None else "available",
        checked_at=at("09:36"),
    )


# ---------------------------------------------------------------------------
# B1. Config defaults and validation
# ---------------------------------------------------------------------------


def test_defaults_match_plan() -> None:
    backlog = make(BacklogRule)
    sla = make(SlaRule)
    assert (backlog.warning_above, backlog.critical_above) == (15, 20)
    assert (sla.resolve_ratio, sla.warning_ratio, sla.critical_ratio) == (0.75, 0.80, 1.0)
    assert make(AdherenceRule).min_duration_sec == 600
    assert make(LongCallRule).min_duration_sec == 2700
    assert backlog.enabled is True


def test_missing_scope_rejected() -> None:
    for rule_class in RULE_CLASSES:
        with pytest.raises(ValidationError):
            rule_class(rule_id="r", recipient="lead_billing")


@pytest.mark.parametrize(
    ("rule_class", "fields"),
    [
        (BacklogRule, {"warning_above": 20, "critical_above": 20}),
        (BacklogRule, {"warning_above": 25, "critical_above": 20}),
        (BacklogRule, {"warning_above": -1}),
        (BacklogRule, {"warning_above": "15"}),
        (BacklogRule, {"critical_above": True}),
        (SlaRule, {"resolve_ratio": 0.80}),
        (SlaRule, {"warning_ratio": 1.1}),
        (SlaRule, {"resolve_ratio": 0}),
        (SlaRule, {"critical_ratio": float("inf")}),
        (SlaRule, {"warning_ratio": float("nan")}),
        (SlaRule, {"resolve_ratio": float("nan")}),
        (BacklogRule, {"critical_above": float("inf")}),
        (LongCallRule, {"min_duration_sec": float("inf")}),
        (AdherenceRule, {"min_duration_sec": 0}),
        (LongCallRule, {"min_duration_sec": -60}),
        (BacklogRule, {"surprise": 1}),
        (BacklogRule, {"rule_id": ""}),
        (BacklogRule, {"scope": {"queues": []}}),
        (BacklogRule, {"scope": {"queues": [""]}}),
        (BacklogRule, {"scope": "everything"}),
        (BacklogRule, {"scope": {"agents": ["a_11"]}}),
        (LongCallRule, {"scope": {"queues": ["billing"]}}),
    ],
    ids=[
        "backlog-warning-equals-critical",
        "backlog-warning-above-critical",
        "backlog-negative",
        "backlog-string-number",
        "backlog-bool-number",
        "sla-resolve-not-below-warning",
        "sla-warning-above-critical",
        "sla-zero-ratio",
        "sla-infinite-critical",
        "sla-nan-warning",
        "sla-nan-resolve",
        "backlog-infinite-threshold",
        "long-call-infinite-duration",
        "adherence-zero-duration",
        "long-call-negative-duration",
        "extra-field",
        "empty-rule-id",
        "empty-scope-list",
        "empty-scope-id",
        "unknown-scope-string",
        "agent-scope-on-queue-rule",
        "queue-scope-on-agent-rule",
    ],
)
def test_invalid_config_rejected(rule_class: type, fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        make(rule_class, **fields)


@pytest.mark.parametrize(
    ("rule_class", "scope"),
    [
        (BacklogRule, "all_owned"),
        (SlaRule, {"queues": ["billing", "vip"]}),
        (LongCallRule, {"agents": ["a_11"]}),
        (AdherenceRule, "all_owned"),
    ],
)
def test_valid_scopes_accepted_and_stored(rule_class: type, scope: Any) -> None:
    rule = make(rule_class, scope=scope)
    stored = rule.scope if isinstance(rule.scope, str) else rule.scope.model_dump()
    assert stored == scope


def test_rule_config_is_frozen() -> None:
    rule = make(BacklogRule)
    with pytest.raises(ValidationError):
        rule.warning_above = 5


# ---------------------------------------------------------------------------
# B2/B3. Threshold rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tickets_waiting", "expected"),
    [(15, "clear"), (16, "warning"), (20, "warning"), (21, "critical")],
)
def test_backlog_boundaries(tickets_waiting: int, expected: str) -> None:
    queue = snapshot("10:00", tickets_waiting=tickets_waiting)
    assert evaluate(make(BacklogRule), queue, at("10:00")) == Decision(expected)


@pytest.mark.parametrize(
    ("longest_wait_sec", "target", "expected"),
    [
        (90, 120, "clear"),
        (91, 120, "hold"),
        (95, 120, "hold"),
        (96, 120, "warning"),
        (120, 120, "warning"),
        (121, 120, "critical"),
        (225, 300, "clear"),
        (240, 300, "warning"),
        (301, 300, "critical"),
    ],
)
def test_sla_boundaries(longest_wait_sec: int, target: int, expected: str) -> None:
    queue = snapshot("10:00", longest_wait_sec=longest_wait_sec, sla_target_sec=target)
    assert evaluate(make(SlaRule), queue, at("10:00")) == Decision(expected)


# ---------------------------------------------------------------------------
# B4. Duration rules: condition at the replay clock, with the deadline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("since", "clock", "expected"),
    [
        ("09:10", "09:54:59", Decision("pending", at("09:55"))),
        ("09:10", "09:55", Decision("met", at("09:55"))),
        (None, "10:00", Decision("clear")),
        ("09:40", "10:30", Decision("met", at("10:25"))),
    ],
    ids=["before-deadline", "at-deadline", "not-on-call", "late-but-fresh"],
)
def test_long_call(since: str | None, clock: str, expected: Decision) -> None:
    assert evaluate(make(LongCallRule), on_call_since(since), at(clock)) == expected


@pytest.mark.parametrize(
    ("start", "clock", "expected"),
    [
        ("09:35", "09:44:59", Decision("pending", at("09:45"))),
        ("09:35", "09:45", Decision("met", at("09:45"))),
        (None, "10:00", Decision("clear")),
        ("10:05", "10:30", Decision("met", at("10:15"))),
    ],
    ids=["before-deadline", "at-deadline", "not-in-violation", "late-but-fresh"],
)
def test_adherence(start: str | None, clock: str, expected: Decision) -> None:
    assert evaluate(make(AdherenceRule), violation_since(start), at(clock)) == expected


# ---------------------------------------------------------------------------
# B5. Configurability, disabled rules and safety
# ---------------------------------------------------------------------------


def test_custom_thresholds_change_decisions() -> None:
    queue = snapshot("10:00", tickets_waiting=6)
    assert evaluate(make(BacklogRule, warning_above=5), queue, at("10:00")) == Decision("warning")

    rule = make(LongCallRule, min_duration_sec=1800)
    assert evaluate(rule, on_call_since("09:10"), at("09:40")) == Decision("met", at("09:40"))


@pytest.mark.parametrize(
    ("rule_class", "subject"),
    [
        (BacklogRule, snapshot("10:00", tickets_waiting=21)),
        (LongCallRule, on_call_since("09:10")),
    ],
)
def test_disabled_rule_is_evaluated_normally(rule_class: type, subject: Any) -> None:
    enabled = evaluate(make(rule_class), subject, at("10:00"))
    disabled = evaluate(make(rule_class, enabled=False), subject, at("10:00"))
    assert disabled == enabled
    assert disabled.condition != "clear"


def test_null_forecast_does_not_affect_queue_rules() -> None:
    with_forecast = snapshot("10:00", tickets_waiting=18, longest_wait_sec=130, forecast=38)
    without_forecast = snapshot("10:00", tickets_waiting=18, longest_wait_sec=130, forecast=None)
    for rule in (make(BacklogRule), make(SlaRule)):
        assert evaluate(rule, with_forecast, at("10:00")) == evaluate(rule, without_forecast, at("10:00"))


@pytest.mark.parametrize(
    ("rule_class", "subject"),
    [
        (BacklogRule, on_call_since("09:10")),
        (LongCallRule, snapshot("10:00")),
        (AdherenceRule, on_call_since("09:10")),
    ],
    ids=["backlog-on-agent", "long-call-on-queue", "adherence-on-call-state"],
)
def test_wrong_subject_type_raises(rule_class: type, subject: Any) -> None:
    with pytest.raises(TypeError):
        evaluate(make(rule_class), subject, at("10:00"))


def test_same_inputs_give_same_decision() -> None:
    rule, subject, clock = make(LongCallRule), on_call_since("09:10"), at("10:00")
    assert evaluate(rule, subject, clock) == evaluate(rule, subject, clock)
