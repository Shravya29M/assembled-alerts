"""Incident lifecycle: records and the pure transition function.

transition(current, decision, ...) decides how one rule x subject incident
changes given a rule Decision. It reads no clock and keeps no counters:
the engine supplies the time (`at`) and the ID for a new incident, so the
same inputs always give the same outputs.

Noise policy (PLAN §5, refined by Decision 106): notify on opening,
warning -> critical escalation and resolution. Same-severity repeats and
critical -> warning downgrades are silent, and each recipient gets at most one
critical notification per incident: a later re-escalation updates the severity
silently. The engine remembers who was told; this function only receives a flag.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from alerts.rules import Decision

Severity = Literal["warning", "critical"] | None  # None for duration rules
Status = Literal["active", "resolved", "disabled"]
ChangeKind = Literal["opened", "escalated", "resolved"]


@dataclass(frozen=True)
class IncidentKey:
    rule_id: str
    subject_id: str
    recipient: str


@dataclass(frozen=True)
class Incident:
    incident_id: str
    rule_id: str
    subject_id: str
    recipient: str
    status: Status
    severity: Severity
    opened_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True)
class Change:
    """A transition that deserves a notification."""

    kind: ChangeKind
    severity: Severity
    effective_at: datetime


@dataclass(frozen=True)
class Notification:
    seq: int  # global position in emitted order, starting at 1
    incident_id: str
    rule_id: str
    rule_version: int
    subject_id: str
    recipient: str
    kind: ChangeKind
    severity: Severity
    effective_at: datetime  # when the condition became true (event ts or timer deadline)
    emitted_at: datetime  # the replay clock when the engine could know it
    line_number: int | None  # the event whose arrival produced it; None at end of input


def transition(
    current: Incident | None,
    decision: Decision,
    *,
    key: IncidentKey,
    at: datetime,
    new_incident_id: str,
    critical_notified: bool = False,
) -> tuple[Incident | None, Change | None]:
    """Next incident and the notifiable change, if any. `current` is the ACTIVE incident or None.

    `critical_notified`: the current incident's recipient has already received a
    critical notification for it, so a warning -> critical escalation is silent.
    """
    condition = decision.condition

    if current is None:
        if condition in ("warning", "critical", "met"):
            severity: Severity = None if condition == "met" else condition
            opened = Incident(
                incident_id=new_incident_id,
                rule_id=key.rule_id,
                subject_id=key.subject_id,
                recipient=key.recipient,
                status="active",
                severity=severity,
                opened_at=at,
                closed_at=None,
            )
            return opened, Change("opened", severity, at)
        return None, None  # clear, hold, pending: nothing to open

    if condition == "clear":
        resolved = replace(current, status="resolved", closed_at=at)
        return resolved, Change("resolved", current.severity, at)  # keeps its last severity
    if condition == "critical" and current.severity == "warning":
        escalated = replace(current, severity="critical")  # the severity is always updated
        return escalated, None if critical_notified else Change("escalated", "critical", at)
    if condition in ("warning", "hold") and current.severity == "critical":
        return replace(current, severity="warning"), None  # silent downgrade
    return current, None  # unchanged severity; pending/met while active is ignored
