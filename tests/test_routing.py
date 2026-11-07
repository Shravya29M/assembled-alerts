"""Checkpoint 3.2 — scope resolution, routing validation, unowned subjects."""

import pytest

from alerts.engine import Engine
from alerts.routing import AGENT_OWNERS, QUEUE_OWNERS, RoutingError, in_scope, seeded_rules, validate_routing
from alerts.rules import AdherenceRule, BacklogRule, LongCallRule, SlaRule
from tests.builders import snapshot, state_change
from tests.engine_builders import Driver, seeded

# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_id", "covered", "not_covered"),
    [
        ("backlog.lead_billing", ["billing", "vip"], ["tier_2", "a_19"]),
        ("sla.lead_support", ["tier_2"], ["billing", "a_11"]),
        ("long_call.lead_billing", ["a_31", "a_19", "a_07", "a_05"], ["a_11", "billing"]),
        ("adherence.lead_support", ["a_23", "a_42", "a_11", "a_88"], ["a_19", "tier_2"]),
    ],
)
def test_all_owned_covers_only_owned_subjects_of_the_right_kind(
    rule_id: str, covered: list[str], not_covered: list[str]
) -> None:
    rule = seeded(rule_id)
    assert all(in_scope(rule, subject) for subject in covered)
    assert not any(in_scope(rule, subject) for subject in not_covered)


def test_explicit_scope_covers_exactly_the_listed_subjects() -> None:
    rule = LongCallRule(rule_id="watch", recipient="lead_support", scope={"agents": ["a_11"]})
    assert in_scope(rule, "a_11")
    assert not in_scope(rule, "a_23")


# ---------------------------------------------------------------------------
# Validation (Decision 41)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"recipient": "lead_x", "scope": "all_owned"},
        {"recipient": "lead_support", "scope": {"agents": ["a_111"]}},
        {"recipient": "lead_support", "scope": {"agents": ["a_19"]}},
        {"recipient": "lead_support", "scope": "all_owned", "cross_owner": True},
    ],
    ids=["unknown-recipient", "unknown-subject", "not-owned-without-flag", "cross-owner-with-all-owned"],
)
def test_invalid_routing_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(RoutingError):
        validate_routing(LongCallRule(rule_id="r", **fields))


def test_unknown_queue_in_explicit_scope_rejected() -> None:
    with pytest.raises(RoutingError):
        validate_routing(BacklogRule(rule_id="r", recipient="lead_billing", scope={"queues": ["chat"]}))


def test_cross_owner_subject_accepted_with_flag() -> None:
    validate_routing(
        LongCallRule(rule_id="r", recipient="lead_billing", scope={"agents": ["a_11"]}, cross_owner=True)
    )


def test_engine_rejects_invalid_routing_at_construction() -> None:
    bad = LongCallRule(rule_id="r", recipient="lead_support", scope={"agents": ["a_19"]})
    with pytest.raises(RoutingError):
        Engine([*seeded_rules(), bad])


def test_cross_owner_rule_notifies_its_own_recipient() -> None:
    watch = LongCallRule(rule_id="long_call.watch_a11", recipient="lead_billing",
                         scope={"agents": ["a_11"]}, cross_owner=True)  # fmt: skip
    driver = Driver([*seeded_rules(), watch])
    driver.send(state_change("09:10", "a_11", "on_call"))
    emitted = driver.send(snapshot("09:56", "tier_2"))
    assert {(n.rule_id, n.recipient) for n in emitted} == {
        ("long_call.watch_a11", "lead_billing"),
        ("long_call.lead_support", "lead_support"),
    }


# ---------------------------------------------------------------------------
# Seeded rules (Decision 40)
# ---------------------------------------------------------------------------


def test_eight_seeded_rules() -> None:
    rules = seeded_rules()
    assert sorted(rule.rule_id for rule in rules) == sorted(
        f"{template}.{lead}"
        for template in ("backlog", "sla", "adherence", "long_call")
        for lead in ("lead_billing", "lead_support")
    )
    for rule in rules:
        validate_routing(rule)


@pytest.mark.parametrize(
    ("rule_class", "owners"),
    [(BacklogRule, QUEUE_OWNERS), (SlaRule, QUEUE_OWNERS), (AdherenceRule, AGENT_OWNERS), (LongCallRule, AGENT_OWNERS)],
)
def test_every_owned_subject_covered_by_exactly_one_seeded_rule(rule_class: type, owners: dict[str, str]) -> None:
    rules = [rule for rule in seeded_rules() if isinstance(rule, rule_class)]
    for subject in owners:
        assert sum(in_scope(rule, subject) for rule in rules) == 1


# ---------------------------------------------------------------------------
# Unowned subjects (Decision 42)
# ---------------------------------------------------------------------------


def test_unowned_agent_is_diagnosed_once_and_never_notified() -> None:
    driver = Driver()
    driver.send(
        state_change("09:00", "a_99", "on_call"),
        state_change("09:10", "a_99", "available", previous_state="on_call"),
        state_change("09:20", "a_99", "on_call", previous_state="available"),
    )
    driver.send(snapshot("10:30", "tier_2"))
    driver.finish()
    assert [(d.kind, d.subject_id, d.line_number) for d in driver.engine.diagnostics] == [
        ("unowned_subject", "a_99", 1)
    ]
    assert driver.engine.notifications == []


def test_unowned_queue_is_diagnosed_and_never_notified() -> None:
    driver = Driver()
    driver.send(snapshot("09:30", "chat", tickets_waiting=30, longest_wait_sec=500))
    assert [(d.kind, d.subject_id) for d in driver.engine.diagnostics] == [("unowned_subject", "chat")]
    assert driver.engine.notifications == []
