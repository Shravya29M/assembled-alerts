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


def _listed(rule: Rule) -> list[str]:
    """The subjects named by an explicit scope. Its kind already matches the rule (rules.py)."""
    assert rule.scope != "all_owned"
    return rule.scope.queues if isinstance(rule, (BacklogRule, SlaRule)) else rule.scope.agents


def in_scope(rule: Rule, subject_id: str) -> bool:
    """Does this rule cover this subject? Queue rules never cover agents, and vice versa."""
    if rule.scope == "all_owned":
        return _owners_for(rule).get(subject_id) == rule.recipient
    return subject_id in _listed(rule)


def validate_routing(rule: Rule) -> None:
    """Reject a rule whose recipient or scope does not fit the ownership map (Decision 41)."""
    if rule.recipient not in LEADS:
        raise RoutingError(f"{rule.rule_id}: unknown recipient {rule.recipient!r}")
    if rule.scope == "all_owned":
        if rule.cross_owner:
            raise RoutingError(f"{rule.rule_id}: cross_owner needs an explicit scope; "
                               "all_owned covers only the recipient's own subjects")
        return
    owners = _owners_for(rule)
    for subject_id in _listed(rule):
        owner = owners.get(subject_id)
        if owner is None:
            raise RoutingError(f"{rule.rule_id}: unknown subject {subject_id!r}")
        if owner != rule.recipient and not rule.cross_owner:
            raise RoutingError(f"{rule.rule_id}: {subject_id} is owned by {owner}, not {rule.recipient}; "
                               "set cross_owner to target it deliberately")


def owner_of(subject_id: str, *, is_queue: bool) -> str | None:
    """The seeded lead for a queue or agent, or None if nobody owns it."""
    return (QUEUE_OWNERS if is_queue else AGENT_OWNERS).get(subject_id)


def seeded_rules() -> list[Rule]:
    """Four templates x two leads, each covering all of its lead's subjects, with PLAN §4 defaults."""
    rules: list[Rule] = []
    for lead in LEADS:
        rules.append(BacklogRule(rule_id=f"backlog.{lead}", recipient=lead, scope="all_owned"))
        rules.append(SlaRule(rule_id=f"sla.{lead}", recipient=lead, scope="all_owned"))
        rules.append(AdherenceRule(rule_id=f"adherence.{lead}", recipient=lead, scope="all_owned"))
        rules.append(LongCallRule(rule_id=f"long_call.{lead}", recipient=lead, scope="all_owned"))
    return rules
