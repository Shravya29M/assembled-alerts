"""Helpers for driving the Phase 3 engine in tests.

Kept separate from tests/builders.py so the Phase 2 tests never import
Phase 3 modules.
"""

from datetime import datetime
from typing import Any

from alerts.engine import Engine
from alerts.incidents import Notification
from alerts.ingest import SourcedEvent
from alerts.models import Event
from alerts.routing import seeded_rules
from alerts.rules import Rule


def hm(value: datetime | None) -> str | None:
    """09:55:00 -> '09:55'; 10:10:30 -> '10:10:30'."""
    if value is None:
        return None
    return value.strftime("%H:%M:%S" if value.second else "%H:%M")


def seeded(rule_id: str) -> Rule:
    return next(rule for rule in seeded_rules() if rule.rule_id == rule_id)


def edited(rule_id: str, **changes: Any) -> Rule:
    """A seeded rule with changed fields, built through full validation."""
    rule = seeded(rule_id)
    return type(rule)(**{**rule.model_dump(), **changes})


def brief(notifications: list[Notification]) -> list[tuple[Any, ...]]:
    """(rule_id, subject_id, kind, severity, effective, emitted) per notification."""
    return [
        (n.rule_id, n.subject_id, n.kind, n.severity, hm(n.effective_at), hm(n.emitted_at))
        for n in notifications
    ]


class Driver:
    """Feeds synthetic events to an Engine, numbering lines 1, 2, 3, ..."""

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.engine = Engine(seeded_rules() if rules is None else rules)
        self.line = 0

    def send(self, *events: Event) -> list[Notification]:
        emitted: list[Notification] = []
        for event in events:
            self.line += 1
            emitted.extend(self.engine.step(SourcedEvent(self.line, event)).notifications)
        return emitted

    def finish(self) -> list[Notification]:
        return self.engine.finish()


def engine_snapshot(engine: Engine) -> dict[str, Any]:
    """Everything observable about an engine, for exact before/after comparisons."""
    return {
        "notifications": list(engine.notifications),
        "incidents": dict(engine.incidents),
        "clock": engine.state.clock,
        "timers": engine.pending_timers(),
        "state_diagnostics": list(engine.state.diagnostics),
        "engine_diagnostics": list(engine.diagnostics),
        "rules": dict(engine.rules),
        "versions": {rule_id: engine.rule_version(rule_id) for rule_id in engine.rules},
    }
