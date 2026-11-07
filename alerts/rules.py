"""Rule configs and pure evaluation for the four rule templates.

evaluate(rule, subject_state, clock) looks at one subject's current state and
returns a Decision. It reads nothing else, writes nothing, and knows nothing
about incidents, notifications or timers. Phase 3 decides what to do with the
Decision.

Every rule carries an `enabled` field, but evaluate deliberately ignores it:
returning "clear" for a disabled rule would look like a recovery. Phase 3
(planned) skips disabled rules and closes their incidents as disabled.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alerts.models import NonEmptyStr, NonNegativeInt, PositiveInt, QueueSnapshot
from alerts.state import AdherenceState, CallState

Condition = Literal["clear", "warning", "critical", "hold", "pending", "met"]

PositiveRatio = Annotated[float, Field(gt=0, allow_inf_nan=False)]  # finite and > 0
_STRICT = ConfigDict(strict=True, frozen=True, extra="forbid")


@dataclass(frozen=True)
class Decision:
    condition: Condition
    due_at: datetime | None = None  # duration rules only: when the condition is met


# ---------------------------------------------------------------------------
# Scope: which subjects a rule covers. Phase 3 resolves it to real subjects.
# ---------------------------------------------------------------------------


class QueueScope(BaseModel):
    model_config = _STRICT
    queues: Annotated[list[NonEmptyStr], Field(min_length=1)]


class AgentScope(BaseModel):
    model_config = _STRICT
    agents: Annotated[list[NonEmptyStr], Field(min_length=1)]


AllOwned = Literal["all_owned"]  # every subject the recipient owns (Phase 3)


# ---------------------------------------------------------------------------
# Rule configs. Defaults are the PLAN §4 values; scope is always required.
# ---------------------------------------------------------------------------


class _RuleBase(BaseModel):
    model_config = _STRICT
    rule_id: NonEmptyStr
    recipient: NonEmptyStr  # the lead who is notified; checked against ownership in routing.py
    cross_owner: bool = False  # True allows an explicit scope to name subjects the recipient does not own
    enabled: bool = True


class BacklogRule(_RuleBase):
    scope: AllOwned | QueueScope
    warning_above: NonNegativeInt = 15
    critical_above: NonNegativeInt = 20

    @model_validator(mode="after")
    def _warning_below_critical(self) -> Self:
        if self.warning_above >= self.critical_above:
            raise ValueError("warning_above must be less than critical_above")
        return self


class SlaRule(_RuleBase):
    scope: AllOwned | QueueScope
    resolve_ratio: PositiveRatio = 0.75
    warning_ratio: PositiveRatio = 0.80
    critical_ratio: PositiveRatio = 1.0

    @model_validator(mode="after")
    def _ratios_ordered(self) -> Self:
        if not self.resolve_ratio < self.warning_ratio <= self.critical_ratio:
            raise ValueError("ratios must satisfy resolve_ratio < warning_ratio <= critical_ratio")
        return self


class AdherenceRule(_RuleBase):
    scope: AllOwned | AgentScope
    min_duration_sec: PositiveInt = 600


class LongCallRule(_RuleBase):
    scope: AllOwned | AgentScope
    min_duration_sec: PositiveInt = 2700


Rule = BacklogRule | SlaRule | AdherenceRule | LongCallRule
SubjectState = QueueSnapshot | CallState | AdherenceState


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------


def evaluate(rule: Rule, subject_state: SubjectState, clock: datetime) -> Decision:
    """Judge one subject against one rule at the replay clock. Ignores rule.enabled."""
    if isinstance(rule, BacklogRule) and isinstance(subject_state, QueueSnapshot):
        return _backlog(rule, subject_state)
    if isinstance(rule, SlaRule) and isinstance(subject_state, QueueSnapshot):
        return _sla(rule, subject_state)
    if isinstance(rule, LongCallRule) and isinstance(subject_state, CallState):
        return _duration(subject_state.on_call_since, rule.min_duration_sec, clock)
    if isinstance(rule, AdherenceRule) and isinstance(subject_state, AdherenceState):
        start = subject_state.effective_start if subject_state.in_violation else None
        return _duration(start, rule.min_duration_sec, clock)
    raise TypeError(f"{type(rule).__name__} cannot evaluate {type(subject_state).__name__}")


def _backlog(rule: BacklogRule, queue: QueueSnapshot) -> Decision:
    waiting = queue.tickets_waiting
    if waiting > rule.critical_above:
        return Decision("critical")
    if waiting > rule.warning_above:
        return Decision("warning")
    return Decision("clear")


def _sla(rule: SlaRule, queue: QueueSnapshot) -> Decision:
    # Divide rather than multiply the ratio by the target, so exact
    # boundaries such as 90/120 == 0.75 compare equal.
    ratio = queue.longest_wait_sec / queue.sla_target_sec
    if ratio > rule.critical_ratio:
        return Decision("critical")
    if ratio >= rule.warning_ratio:
        return Decision("warning")
    if ratio <= rule.resolve_ratio:
        return Decision("clear")
    return Decision("hold")  # between resolve and warning: keep whatever is current


def _duration(start: datetime | None, min_duration_sec: int, clock: datetime) -> Decision:
    if start is None:
        return Decision("clear")
    due_at = start + timedelta(seconds=min_duration_sec)
    return Decision("met" if clock >= due_at else "pending", due_at)
