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
from dataclasses import dataclass, replace
from datetime import datetime

from alerts.incidents import Change, Incident, IncidentKey, Notification, transition
from alerts.ingest import SourcedEvent
from alerts.models import AdherenceCheck, AgentStateChange, QueueSnapshot
from alerts.routing import in_scope, owner_of, validate_routing
from alerts.rules import AdherenceRule, BacklogRule, Decision, LongCallRule, Rule, SlaRule, evaluate
from alerts.state import Diagnostic, Outcome, ReplayState
from alerts.timers import TimerQueue


class ReplayFinishedError(RuntimeError):
    """step() was called after finish(); call reset() to replay again."""


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


def _validated(rule: Rule) -> Rule:
    """Re-run full Pydantic validation: model_copy() and model_construct() skip it."""
    return type(rule).model_validate(rule.model_dump())


class Engine:
    def __init__(self, rules: list[Rule], *, versions: dict[str, int] | None = None) -> None:
        """`versions` restores rule versions (e.g. a stored baseline after reset); default 1 each."""
        rules = [_validated(rule) for rule in rules]  # validate everything before any state exists
        seen: set[str] = set()
        for rule in rules:
            if rule.rule_id in seen:
                raise ValueError(f"duplicate rule_id {rule.rule_id!r}")
            seen.add(rule.rule_id)
            validate_routing(rule)  # RoutingError
        self.rules: dict[str, Rule] = {rule.rule_id: rule for rule in rules}
        self._versions: dict[str, int] = {rule.rule_id: 1 for rule in rules} if versions is None else dict(versions)
        if set(self._versions) != set(self.rules) or min(self._versions.values(), default=1) < 1:
            raise ValueError("versions must give every rule exactly one version >= 1")
        self.reset()

    def reset(self) -> None:
        """Forget all replay state. Keeps the current rule configs and versions (not factory defaults)."""
        self.state = ReplayState()
        self.notifications: list[Notification] = []
        self.incidents: dict[str, Incident] = {}  # incident_id -> latest version, in creation order
        self.diagnostics: list[Diagnostic] = []  # engine-level; ReplayState keeps its own
        self._timers = TimerQueue()
        # (rule_id, subject_id) -> incident_id of its ACTIVE incident. No recipient: PLAN §5 allows
        # one active incident per rule x subject, even if the rule's recipient is edited.
        self._active: dict[tuple[str, str], str] = {}
        self._counts: dict[tuple[str, str], int] = {}  # (rule_id, subject_id) -> incidents so far
        self._unowned: set[str] = set()  # subjects already diagnosed as unowned
        # (incident_id, recipient) pairs that were sent a critical notification (Decision 106).
        # Rebuilt by journal replay on restart and after a failed write; cleared here on reset.
        self._critical_sent: set[tuple[str, str]] = set()
        self._finished = False  # set by finish(); step() is rejected until reset()

    # -- replay -------------------------------------------------------------

    def step(self, sourced: SourcedEvent) -> StepResult:
        if self._finished:
            raise ReplayFinishedError("the replay is finished; call reset() before stepping again")
        event, line = sourced.event, sourced.line_number
        if self.state.check(sourced) != "accepted":
            return StepResult(self.state.apply(sourced), [])  # records the duplicate/stale

        start = len(self.notifications)
        clock = event.ts if self.state.clock is None else max(self.state.clock, event.ts)
        self._fire_timers(clock, line, inclusive=False)  # 1. before the event
        outcome = self.state.apply(sourced)  # 2.
        subject_id = event.queue_id if isinstance(event, QueueSnapshot) else event.agent_id
        self._check_owner(subject_id, isinstance(event, QueueSnapshot), line)
        for rule_id in sorted(self.rules):  # 3.
            rule = self.rules[rule_id]
            if rule.enabled and isinstance(rule, _RULES_FOR_EVENT[type(event)]) and in_scope(rule, subject_id):
                self._evaluate(rule, subject_id, event.ts, line)
        self._fire_timers(clock, line, inclusive=False)  # 4. only late-created timers can be due here
        return StepResult(outcome, self.notifications[start:])

    def finish(self) -> list[Notification]:
        """End of input: fire timers due at or before the horizon (the final clock). Runs once."""
        if self._finished or self.state.clock is None:
            self._finished = True
            return []
        self._finished = True
        start = len(self.notifications)
        self._fire_timers(self.state.clock, None, inclusive=True)
        return self.notifications[start:]

    def run_all(self, events: Iterable[SourcedEvent]) -> None:
        for sourced in events:
            self.step(sourced)
        self.finish()

    # -- rule management (never notifies, never rewrites history) -----------

    def disable_rule(self, rule_id: str) -> list[Notification]:
        return self.update_rule(self._rule(rule_id).model_copy(update={"enabled": False}))

    def enable_rule(self, rule_id: str) -> list[Notification]:
        return self.update_rule(self._rule(rule_id).model_copy(update={"enabled": True}))

    def update_rule(self, rule: Rule) -> list[Notification]:
        """Replace a rule's config and bump its version. Always returns [] (Decisions 45, 46, 60).

        Active incidents the new config no longer covers (disabled, or out of scope)
        close as `disabled`; covered ones keep their ID and take the new recipient.
        Duration timers are recomputed from current state; after finish() existing
        timers are only moved or cancelled, never added (Decision 68).
        Queue rules simply use the new config at their next snapshot.
        """
        rule = self.validate_rule(rule)  # all checks run before any state changes
        self.rules[rule.rule_id] = rule
        self._versions[rule.rule_id] += 1
        self._reconcile(rule)
        if rule.enabled and isinstance(rule, (LongCallRule, AdherenceRule)):
            if self._finished:
                self._retime_after_finish(rule)
            else:
                self._recompute_timers(rule)
        return []

    def validate_rule(self, rule: Rule) -> Rule:
        """Every check update_rule makes, with no side effects. Returns the validated rule.

        Raises ValidationError (config), ValueError (unknown rule_id) or RoutingError.
        """
        rule = _validated(rule)
        self._rule(rule.rule_id)  # must already exist
        validate_routing(rule)
        return rule

    def add_rule(self, rule: Rule) -> list[Notification]:
        """Add a new rule at version 1. Always returns []: a new rule never alerts retroactively.

        Queue rules apply from the next snapshot. Duration rules get timers from current
        state, max(due, now), like an edit (Decision 45); after finish() nothing is
        scheduled until reset() (Decisions 66, 68).
        """
        rule = self.validate_new_rule(rule)  # all checks run before any state changes
        self.rules[rule.rule_id] = rule
        self._versions[rule.rule_id] = 1
        if rule.enabled and isinstance(rule, (LongCallRule, AdherenceRule)) and not self._finished:
            self._recompute_timers(rule)
        return []

    def validate_new_rule(self, rule: Rule) -> Rule:
        """Every check add_rule makes, with no side effects. Returns the validated rule.

        Raises ValidationError (config), ValueError (duplicate rule_id) or RoutingError.
        """
        rule = _validated(rule)
        if rule.rule_id in self.rules:
            raise ValueError(f"duplicate rule_id {rule.rule_id!r}")
        validate_routing(rule)
        return rule

    def new_rule_id(self, template: str, recipient: str) -> str:
        """`{template}.{recipient}.{n}` with the smallest unused n >= 2 (the seeded rule is n = 1)."""
        n = 2
        while f"{template}.{recipient}.{n}" in self.rules:
            n += 1
        return f"{template}.{recipient}.{n}"

    # -- views --------------------------------------------------------------

    @property
    def finished(self) -> bool:
        """True after finish(), until reset()."""
        return self._finished

    def rule_version(self, rule_id: str) -> int:
        return self._versions[rule_id]

    def active_incidents(self) -> list[Incident]:
        return [incident for incident in self.incidents.values() if incident.status == "active"]

    def pending_timers(self) -> dict[tuple[str, str], datetime]:
        return self._timers.live()

    # -- internals ----------------------------------------------------------

    def _rule(self, rule_id: str) -> Rule:
        if rule_id not in self.rules:
            raise ValueError(f"unknown rule_id {rule_id!r}")
        return self.rules[rule_id]

    def _covers(self, rule: Rule, subject_id: str) -> bool:
        return rule.enabled and in_scope(rule, subject_id)

    def _reconcile(self, rule: Rule) -> None:
        """Bring this rule's active incidents and timers in line with its new config."""
        for pair, incident_id in list(self._active.items()):
            if pair[0] != rule.rule_id:
                continue
            incident = self.incidents[incident_id]
            if self._covers(rule, pair[1]):
                self.incidents[incident_id] = replace(incident, recipient=rule.recipient)
            else:  # stopped watching: closed silently, not recovered
                assert self.state.clock is not None  # an active incident implies an event
                self.incidents[incident_id] = replace(incident, status="disabled", closed_at=self.state.clock)
                del self._active[pair]
        for key in self._timers.live():
            if key[0] == rule.rule_id and not self._covers(rule, key[1]):
                self._timers.cancel(key)

    def _recompute_timers(self, rule: LongCallRule | AdherenceRule) -> None:
        """Re-derive each covered subject's deadline from current state: max(due, now) (Decision 45)."""
        clock = self.state.clock
        if clock is None:
            return
        states = self.state.calls if isinstance(rule, LongCallRule) else self.state.adherence
        for subject_id in sorted(states):  # sorted: deterministic creation order
            key = (rule.rule_id, subject_id)
            if not in_scope(rule, subject_id) or key in self._active:
                continue  # an ACTIVE incident ends only when its episode ends
            decision = evaluate(rule, states[subject_id], clock)
            if decision.due_at is None:  # clear
                self._timers.cancel(key)
            elif self._timers.live().get(key) != max(decision.due_at, clock):
                self._timers.schedule(key, max(decision.due_at, clock))

    def _retime_after_finish(self, rule: LongCallRule | AdherenceRule) -> None:
        """Finalized replay: move each existing timer to its new deadline if that is still after
        the final clock, otherwise cancel it. Never adds a timer, and nothing fires (Decision 68)."""
        clock = self.state.clock
        states = self.state.calls if isinstance(rule, LongCallRule) else self.state.adherence
        for key, deadline in self._timers.live().items():
            if key[0] != rule.rule_id:
                continue  # _reconcile already cancelled this rule's uncovered subjects
            assert clock is not None  # a timer implies an applied event
            due = evaluate(rule, states[key[1]], clock).due_at
            if due is not None and due > clock:
                if due != deadline:
                    self._timers.schedule(key, due)
            else:
                self._timers.cancel(key)

    def _check_owner(self, subject_id: str, is_queue: bool, line: int) -> None:
        """Diagnose a subject with no seeded owner once; never guess a recipient (Decision 42)."""
        if owner_of(subject_id, is_queue=is_queue) is None and subject_id not in self._unowned:
            self._unowned.add(subject_id)
            self.diagnostics.append(Diagnostic(
                "unowned_subject", subject_id, line, "no lead owns this subject; no notification is routed",
            ))  # fmt: skip

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
        elif timer_key not in self._active:
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
        pair = (rule.rule_id, subject_id)
        current_id = self._active.get(pair)
        current = self.incidents[current_id] if current_id is not None else None
        key = IncidentKey(rule.rule_id, subject_id, rule.recipient)
        new_id = f"{rule.rule_id}:{subject_id}:{self._counts.get(pair, 0) + 1}"

        critical_notified = current is not None and (current.incident_id, current.recipient) in self._critical_sent
        incident, change = transition(current, decision, key=key, at=effective_at, new_incident_id=new_id,
                                      critical_notified=critical_notified)  # fmt: skip
        if incident is None:
            return
        self.incidents[incident.incident_id] = incident
        if incident.status == "active":
            self._active[pair] = incident.incident_id
        else:
            del self._active[pair]
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
        if change.kind in ("opened", "escalated") and change.severity == "critical":
            self._critical_sent.add((incident.incident_id, incident.recipient))
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
