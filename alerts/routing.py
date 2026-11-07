"""Seeded team-lead ownership, scope resolution and the seeded rules.

The ownership maps are illustrative demo data (PLAN §3), not derived from
the source events. Agent rules use agent ownership only; queue membership
in the events is never used to guess an owner.
"""

from alerts.rules import AdherenceRule, BacklogRule, LongCallRule, Rule, SlaRule

QUEUE_OWNERS: dict[str, str] = {
    "billing": "lead_billing",
    "vip": "lead_billing",
    "tier_2": "lead_support",
}

AGENT_OWNERS: dict[str, str] = {
    "a_31": "lead_billing",
    "a_19": "lead_billing",
    "a_07": "lead_billing",
    "a_05": "lead_billing",
    "a_23": "lead_support",
    "a_42": "lead_support",
    "a_11": "lead_support",
    "a_88": "lead_support",
}

LEADS = ("lead_billing", "lead_support")


class RoutingError(ValueError):
    """A rule's recipient or scope does not fit the ownership map."""


def _owners_for(rule: Rule) -> dict[str, str]:
    return QUEUE_OWNERS if isinstance(rule, (BacklogRule, SlaRule)) else AGENT_OWNERS


def in_scope(rule: Rule, subject_id: str) -> bool:
    """Does this rule cover this subject? Queue rules never cover agents, and vice versa."""
    if rule.scope == "all_owned":
        return _owners_for(rule).get(subject_id) == rule.recipient
    # An explicit scope's kind already matches the rule (validated in rules.py).
    listed = rule.scope.queues if isinstance(rule, (BacklogRule, SlaRule)) else rule.scope.agents
    return subject_id in listed


def validate_routing(rule: Rule) -> None:
    """Checkpoint 3.2 — not implemented yet."""
    raise NotImplementedError("validate_routing is part of checkpoint 3.2")


def seeded_rules() -> list[Rule]:
    """Four templates x two leads, each covering all of its lead's subjects, with PLAN §4 defaults."""
    rules: list[Rule] = []
    for lead in LEADS:
        rules.append(BacklogRule(rule_id=f"backlog.{lead}", recipient=lead, scope="all_owned"))
        rules.append(SlaRule(rule_id=f"sla.{lead}", recipient=lead, scope="all_owned"))
        rules.append(AdherenceRule(rule_id=f"adherence.{lead}", recipient=lead, scope="all_owned"))
        rules.append(LongCallRule(rule_id=f"long_call.{lead}", recipient=lead, scope="all_owned"))
    return rules
