"""Checkpoint 3.1 — the pure incident transition.

transition(current, decision, key=..., at=..., new_incident_id=...) returns
(incident or None, change or None). It holds no counters and reads no clock:
the engine supplies the time and the ID for any new incident.
"""

import pytest

from alerts.incidents import Change, Incident, IncidentKey, transition
from alerts.rules import Decision
from tests.builders import at

KEY = IncidentKey(rule_id="backlog.lead_billing", subject_id="billing", recipient="lead_billing")
DURATION_KEY = IncidentKey(rule_id="long_call.lead_support", subject_id="a_11", recipient="lead_support")
NOW = at("09:40")


def active(severity: str | None, key: IncidentKey = KEY) -> Incident:
    return Incident(
        incident_id=f"{key.rule_id}:{key.subject_id}:1",
        rule_id=key.rule_id,
        subject_id=key.subject_id,
        recipient=key.recipient,
        status="active",
        severity=severity,
        opened_at=at("09:30"),
        closed_at=None,
    )


def step(current: Incident | None, condition: str, key: IncidentKey = KEY) -> tuple[Incident | None, Change | None]:
    return transition(current, Decision(condition), key=key, at=NOW, new_incident_id="NEW:1")


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["warning", "critical"])
def test_opens_at_reported_severity(severity: str) -> None:
    incident, change = step(None, severity)
    assert incident == Incident(
        incident_id="NEW:1", rule_id=KEY.rule_id, subject_id="billing", recipient="lead_billing",
        status="active", severity=severity, opened_at=NOW, closed_at=None,
    )  # fmt: skip
    assert change == Change(kind="opened", severity=severity, effective_at=NOW)


@pytest.mark.parametrize("condition", ["clear", "hold", "pending"])
def test_nothing_opens_without_a_breach(condition: str) -> None:
    assert step(None, condition) == (None, None)


def test_duration_met_opens_without_severity() -> None:
    incident, change = step(None, "met", DURATION_KEY)
    assert incident is not None and incident.severity is None and incident.incident_id == "NEW:1"
    assert change == Change(kind="opened", severity=None, effective_at=NOW)


# ---------------------------------------------------------------------------
# Changes while active
# ---------------------------------------------------------------------------


def test_warning_to_critical_escalates() -> None:
    incident, change = step(active("warning"), "critical")
    assert incident == active("critical")
    assert change == Change(kind="escalated", severity="critical", effective_at=NOW)


@pytest.mark.parametrize(
    ("severity", "condition", "expected_severity"),
    [
        ("critical", "warning", "warning"),  # silent downgrade
        ("critical", "hold", "warning"),  # hold band: silent downgrade
        ("warning", "hold", "warning"),
        ("warning", "warning", "warning"),  # no repeat
        ("critical", "critical", "critical"),
    ],
)
def test_silent_changes(severity: str, condition: str, expected_severity: str) -> None:
    incident, change = step(active(severity), condition)
    assert incident == active(expected_severity)
    assert change is None


@pytest.mark.parametrize("severity", ["warning", "critical"])
def test_clear_resolves_with_last_severity(severity: str) -> None:
    incident, change = step(active(severity), "clear")
    assert incident is not None
    assert (incident.status, incident.closed_at, incident.severity) == ("resolved", NOW, severity)
    assert change == Change(kind="resolved", severity=severity, effective_at=NOW)


@pytest.mark.parametrize("condition", ["pending", "met"])
def test_active_duration_incident_ignores_pending_and_met(condition: str) -> None:
    current = active(None, DURATION_KEY)
    assert step(current, condition, DURATION_KEY) == (current, None)


def test_active_duration_incident_resolves_on_clear() -> None:
    incident, change = step(active(None, DURATION_KEY), "clear", DURATION_KEY)
    assert incident is not None and incident.status == "resolved"
    assert change == Change(kind="resolved", severity=None, effective_at=NOW)


# ---------------------------------------------------------------------------
# Purity: no hidden counters, randomness or clock
# ---------------------------------------------------------------------------


def test_same_inputs_give_same_outputs() -> None:
    current = active("warning")
    assert step(current, "critical") == step(current, "critical")
    assert current == active("warning")  # input unchanged


def test_new_incident_id_is_used_verbatim() -> None:
    first, _ = transition(None, Decision("warning"), key=KEY, at=NOW, new_incident_id="x:7")
    second, _ = transition(None, Decision("warning"), key=KEY, at=NOW, new_incident_id="x:7")
    assert first is not None and second is not None
    assert first.incident_id == second.incident_id == "x:7"


def test_times_come_only_from_the_supplied_at() -> None:
    later = at("11:11:11")
    incident, change = transition(None, Decision("critical"), key=KEY, at=later, new_incident_id="x:1")
    assert incident is not None and change is not None
    assert incident.opened_at == change.effective_at == later
