"""Pure helpers that turn engine and store objects into readable text and JSON-ready dicts.

No HTTP, no database and no alert logic: the API calls these after the Store
has done its work. Previews state the same comparisons rules.py makes.
"""

from datetime import datetime
from typing import Any

from pydantic import ValidationError

from alerts.incidents import Incident, Notification
from alerts.routing import AGENT_OWNERS, QUEUE_OWNERS
from alerts.rules import AdherenceRule, BacklogRule, LongCallRule, Rule, SlaRule
from alerts.state import Diagnostic
from alerts.storage import JournalRow

TEMPLATE_NAMES: dict[type, str] = {
    BacklogRule: "backlog",
    SlaRule: "sla",
    AdherenceRule: "adherence",
    LongCallRule: "long_call",
}

# Within one step the event's outcome is decided first, then ReplayState applies
# it, then rules run; so at the same line this is the order things happened.
_SOURCE_ORDER = {"journal": 0, "replay": 1, "engine": 2}


# ---------------------------------------------------------------------------
# Rule previews
# ---------------------------------------------------------------------------


def rule_preview(rule: Rule) -> str:
    """One plain-English sentence that says exactly when this rule notifies."""
    queues = isinstance(rule, (BacklogRule, SlaRule))
    who = _subjects_phrase(rule, queues)
    if isinstance(rule, BacklogRule):
        body = (f"{who} has more than {rule.warning_above} tickets waiting; "
                f"critical above {rule.critical_above}.")  # fmt: skip
    elif isinstance(rule, SlaRule):
        body = (f"the longest wait in {who} reaches {_percent(rule.warning_ratio)} of its SLA target; "
                f"critical above {_percent(rule.critical_ratio)}; "
                f"recovers at or below {_percent(rule.resolve_ratio)}.")  # fmt: skip
    elif isinstance(rule, AdherenceRule):
        body = f"{who} has been out of adherence for at least {_duration(rule.min_duration_sec)}."
    else:
        body = f"{who} has been on one call for at least {_duration(rule.min_duration_sec)}."
    text = f"Notify {rule.recipient} when {body}{_cross_owner_note(rule, queues)}"
    return text if rule.enabled else f"Disabled: {text}"


def _subjects_phrase(rule: Rule, queues: bool) -> str:
    noun = "queue" if queues else "agent"
    if rule.scope == "all_owned":
        owners = QUEUE_OWNERS if queues else AGENT_OWNERS
        owned = sorted(subject for subject, owner in owners.items() if owner == rule.recipient)
        article = "any" if queues else "an"
        return f"{article} {noun} they own ({', '.join(owned)})"
    return f"any listed {noun} ({', '.join(_listed(rule))})"


def _listed(rule: Rule) -> list[str]:
    assert rule.scope != "all_owned"
    return rule.scope.queues if isinstance(rule, (BacklogRule, SlaRule)) else rule.scope.agents


def _cross_owner_note(rule: Rule, queues: bool) -> str:
    """Name the real owner of every listed subject the recipient does not own."""
    if rule.scope == "all_owned":
        return ""
    owners = QUEUE_OWNERS if queues else AGENT_OWNERS
    foreign = [f"{s} is owned by {owners[s]}" for s in _listed(rule) if owners.get(s, rule.recipient) != rule.recipient]
    return f" Cross-owner: {'; '.join(foreign)}." if foreign else ""


def _percent(ratio: float) -> str:
    return f"{round(ratio * 100, 4):g}%"  # 0.8 -> "80%", 0.775 -> "77.5%"


def _duration(seconds: int) -> str:
    if seconds % 60:
        return "1 second" if seconds == 1 else f"{seconds} seconds"
    minutes = seconds // 60
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def merge_diagnostics(
    journal_rows: list[JournalRow], replay_diags: list[Diagnostic], engine_diags: list[Diagnostic]
) -> list[dict[str, Any]]:
    """One list from three sources, ordered by line, then journal -> replay -> engine.

    Duplicate and stale events leave no ReplayState diagnostic of their own
    (a duplicate only does when its payload differs), so they come from the journal.
    """
    rows: list[dict[str, Any]] = []
    for row in journal_rows:
        if row.kind != "event" or row.outcome not in ("duplicate", "stale"):
            continue
        if row.outcome == "duplicate":
            kind, detail = "duplicate_event", f"event_id {row.event_id} was already processed; ignored"
        else:
            kind, detail = "stale_event", "older than the latest event of its type for this subject; ignored"
        rows.append(_diagnostic(row.line_number, kind, row.event_id, detail, "journal"))
    for source, diagnostics in (("replay", replay_diags), ("engine", engine_diags)):
        rows.extend(_diagnostic(d.line_number, d.kind, d.subject_id, d.detail, source) for d in diagnostics)
    # sorted() is stable: rows from one source keep the order they happened in.
    return sorted(rows, key=lambda d: (d["line_number"], _SOURCE_ORDER[d["source"]]))


def _diagnostic(line_number: int | None, kind: str, subject_id: str | None, detail: str, source: str) -> dict[str, Any]:
    return {"line_number": line_number, "kind": kind, "subject_id": subject_id, "detail": detail, "source": source}


# ---------------------------------------------------------------------------
# JSON shaping
# ---------------------------------------------------------------------------


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def rule_json(rule: Rule, version: int) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "template": TEMPLATE_NAMES[type(rule)],
        "version": version,
        "enabled": rule.enabled,
        "preview": rule_preview(rule),
        "config": rule.model_dump(mode="json"),
    }


def notification_json(n: Notification) -> dict[str, Any]:
    return {
        "seq": n.seq,
        "incident_id": n.incident_id,
        "rule_id": n.rule_id,
        "rule_version": n.rule_version,
        "subject_id": n.subject_id,
        "recipient": n.recipient,
        "kind": n.kind,
        "severity": n.severity,
        "effective_at": iso(n.effective_at),
        "emitted_at": iso(n.emitted_at),
        "line_number": n.line_number,
    }


def incident_json(i: Incident) -> dict[str, Any]:
    return {
        "incident_id": i.incident_id,
        "rule_id": i.rule_id,
        "subject_id": i.subject_id,
        "recipient": i.recipient,
        "status": i.status,
        "severity": i.severity,
        "opened_at": iso(i.opened_at),
        "closed_at": iso(i.closed_at),
    }


def describe_validation_error(err: ValidationError) -> str:
    """'field: message' per problem; whole-model checks (empty loc) show just the message."""
    parts = []
    for error in err.errors():
        field = ".".join(str(part) for part in error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        parts.append(f"{field}: {message}" if field else message)
    return "; ".join(parts)
