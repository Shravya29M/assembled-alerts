"""Typed event models: the schema for one line of data/events.jsonl.

Structural validation only. Deduplication, freshness, the clock, state and
rules live in later phases.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Reusable field types. Annotated[base, extra rules] attaches constraints to a type.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
NonEmptyStr = Annotated[str, Field(min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
AgentState = Literal["available", "on_call", "on_break", "in_meeting"]


class _EventBase(BaseModel):
    """Fields and settings shared by every event type."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    event_id: NonEmptyStr
    ts: UtcDatetime


class QueueSnapshot(_EventBase):
    type: Literal["queue_snapshot"]
    queue_id: NonEmptyStr
    tickets_waiting: NonNegativeInt
    longest_wait_sec: NonNegativeInt
    sla_target_sec: PositiveInt
    agents_available: NonNegativeInt
    agents_on_call: NonNegativeInt
    volume_last_15m: NonNegativeInt
    volume_forecast_next_15m: NonNegativeInt | None


class AgentStateChange(_EventBase):
    type: Literal["agent_state_change"]
    agent_id: NonEmptyStr
    queue_ids: list[str] | None
    previous_state: AgentState | None
    previous_state_duration_sec: NonNegativeInt | None
    new_state: AgentState


class AdherenceCheck(_EventBase):
    type: Literal["adherence_check"]
    agent_id: NonEmptyStr
    queue_ids: list[str] | None
    scheduled_state: AgentState
    actual_state: AgentState
    in_violation: bool
    violation_started_at: UtcDatetime | None


# The `type` field picks which model validates a record.
Event = Annotated[
    QueueSnapshot | AgentStateChange | AdherenceCheck,
    Field(discriminator="type"),
]
