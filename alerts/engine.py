"""The replay orchestrator: state, rules, timers, incidents and notifications.

One step, for an event the state will accept:
  1. Fire timers due strictly before the new clock (before the event is applied).
  2. Apply the event to ReplayState.
  3. Evaluate the rules for that event's family and subject, in rule_id order.
  4. Fire timers that step 3 created with a deadline already behind the clock
     (late-but-fresh events, Decision 43).
Duplicate and stale events are recorded by ReplayState and do nothing else.

Rule evaluation stays pure (rules.evaluate, incidents.transition); this
module owns the clock-dependent choices and all mutable bookkeeping.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from alerts.incidents import Change, Incident, IncidentKey, Notification, transition
from alerts.ingest import SourcedEvent
from alerts.models import AdherenceCheck, AgentStateChange, QueueSnapshot
from alerts.routing import in_scope
from alerts.rules import AdherenceRule, BacklogRule, Decision, LongCallRule, Rule, SlaRule, evaluate
from alerts.state import Diagnostic, Outcome, ReplayState
from alerts.timers import TimerQueue


@dataclass(frozen=True)
class StepResult:
    outcome: Outcome
    notifications: list[Notification]


# Which rule types react to which event family.
_RULES_FOR_EVENT: dict[type, tuple[type, ...]] = {
    QueueSnapshot: (BacklogRule, SlaRule),
    AgentStateChange: (LongCallRule,),
    AdherenceCheck: (AdherenceRule,),
}


class Engine:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules: dict[str, Rule] = {rule.rule_id: rule for rule in rules}
        self._versions: dict[str, int] = {rule.rule_id: 1 for rule in rules}
        self.reset()

    def reset(self) -> None:
        """Forget all replay state. Keeps the current rule configs and versions (not factory defaults)."""
        self.state = ReplayState()
        self.notifications: list[Notification] = []
        self.incidents: dict[str, Incident] = {}  # incident_id -> latest version, in creation order
        self.diagnostics: list[Diagnostic] = []  # engine-level; ReplayState keeps its own
        self._timers = TimerQueue()
        self._active: dict[IncidentKey, str] = {}  # key -> incident_id of its ACTIVE incident
        self._counts: dict[tuple[str, str], int] = {}  # (rule_id, subject_id) -> incidents so far

    # -- replay -------------------------------------------------------------

    def step(self, sourced: SourcedEvent) -> StepResult:
        event, line = sourced.event, sourced.line_number
        if self.state.check(sourced) != "accepted":
            return StepResult(self.state.apply(sourced), [])  # records the duplicate/stale

        start = len(self.notifications)
        clock = event.ts if self.state.clock is None else max(self.state.clock, event.ts)
        self._fire_timers(clock, line, inclusive=False)  # 1. before the event
        outcome = self.state.apply(sourced)  # 2.
        subject_id = event.queue_id if isinstance(event, QueueSnapshot) else event.agent_id
        for rule_id in sorted(self.rules):  # 3.
            rule = self.rules[rule_id]
            if rule.enabled and isinstance(rule, _RULES_FOR_EVENT[type(event)]) and in_scope(rule, subject_id):
                self._evaluate(rule, subject_id, event.ts, line)
        self._fire_timers(clock, line, inclusive=False)  # 4. only late-created timers can be due here
        return StepResult(outcome, self.notifications[start:])

    def finish(self) -> list[Notification]:
        """End of input: fire timers due at or before the horizon (the final clock)."""
        if self.state.clock is None:
            return []
        start = len(self.notifications)
        self._fire_timers(self.state.clock, None, inclusive=True)
        return self.notifications[start:]

    def run_all(self, events: Iterable[SourcedEvent]) -> None:
        for sourced in events:
            self.step(sourced)
        self.finish()

    # -- views --------------------------------------------------------------

    def rule_version(self, rule_id: str) -> int:
        return self._versions[rule_id]

    def active_incidents(self) -> list[Incident]:
        return [incident for incident in self.incidents.values() if incident.status == "active"]

    def pending_timers(self) -> dict[tuple[str, str], datetime]:
        return self._timers.live()

    # -- internals ----------------------------------------------------------

    def _evaluate(self, rule: Rule, subject_id: str, event_ts: datetime, line: int) -> None:
        """Evaluate one rule against the subject the current event (at event_ts) just updated."""
        clock = self.state.clock
        assert clock is not None  # an event has been applied
        if isinstance(rule, (BacklogRule, SlaRule)):
            subject_state = self.state.queues[subject_id]
        elif isinstance(rule, LongCallRule):
            subject_state = self.state.calls[subject_id]
        else:
            subject_state = self.state.adherence[subject_id]
        decision = evaluate(rule, subject_state, clock)

        if isinstance(rule, (BacklogRule, SlaRule)):
            self._transition(rule, subject_id, decision, event_ts, clock, line)
            return

        # Duration rules: an incident opens only when its timer fires (Decision 38).
        timer_key = (rule.rule_id, subject_id)
        if decision.condition == "clear":
            self._timers.cancel(timer_key)
            self._transition(rule, subject_id, decision, event_ts, clock, line)
        elif self._key(rule, subject_id) not in self._active:
            # pending or met: (re)schedule unless this deadline is already live, so a
            # repeated observation keeps the timer's original creation order.
            if self._timers.live().get(timer_key) != decision.due_at:
                assert decision.due_at is not None
                self._timers.schedule(timer_key, decision.due_at)
        # pending/met while an incident is ACTIVE: nothing to do (Decision 45).

    def _fire_timers(self, clock: datetime, line: int | None, *, inclusive: bool) -> None:
        for (rule_id, subject_id), deadline in self._timers.pop_due(clock, inclusive=inclusive):
            rule = self.rules[rule_id]
            self._transition(rule, subject_id, Decision("met", deadline), deadline, clock, line)

    def _transition(
        self,
        rule: Rule,
        subject_id: str,
        decision: Decision,
        effective_at: datetime,
        emitted_at: datetime,
        line: int | None,
    ) -> None:
        key = self._key(rule, subject_id)
        current_id = self._active.get(key)
        current = self.incidents[current_id] if current_id is not None else None
        pair = (rule.rule_id, subject_id)
        new_id = f"{rule.rule_id}:{subject_id}:{self._counts.get(pair, 0) + 1}"

        incident, change = transition(current, decision, key=key, at=effective_at, new_incident_id=new_id)
        if incident is None:
            return
        self.incidents[incident.incident_id] = incident
        if incident.status == "active":
            self._active[key] = incident.incident_id
        else:
            del self._active[key]
        if change is None:
            return
        if change.kind == "opened":
            self._counts[pair] = self._counts.get(pair, 0) + 1
        if change.kind == "resolved" and current is not None and effective_at < current.opened_at:
            assert line is not None  # resolutions only come from events
            self.diagnostics.append(Diagnostic(
                "alert_contradicted_by_late_evidence", subject_id, line,
                f"{rule.rule_id}: condition ended at {effective_at.isoformat()}, before the alert's "
                f"effective time {current.opened_at.isoformat()}; the notification is kept",
            ))  # fmt: skip
        self._notify(rule, incident, change, emitted_at, line)

    def _notify(self, rule: Rule, incident: Incident, change: Change, emitted_at: datetime, line: int | None) -> None:
        self.notifications.append(Notification(
            seq=len(self.notifications) + 1,
            incident_id=incident.incident_id,
            rule_id=rule.rule_id,
            rule_version=self._versions[rule.rule_id],
            subject_id=incident.subject_id,
            recipient=incident.recipient,
            kind=change.kind,
            severity=change.severity,
            effective_at=change.effective_at,
            emitted_at=emitted_at,
            line_number=line,
        ))  # fmt: skip

    @staticmethod
    def _key(rule: Rule, subject_id: str) -> IncidentKey:
        return IncidentKey(rule.rule_id, subject_id, rule.recipient)
