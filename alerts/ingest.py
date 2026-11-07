"""Parse JSONL lines into validated events and load a whole file.

Structural validation only: no deduplication, freshness checks, clock, state
or rules. A bad line is recorded as rejected and loading continues.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from alerts.models import Event

# A TypeAdapter validates a type that is not itself a BaseModel (here, the union).
_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)

_JSON_TYPE_NAMES = {list: "array", str: "string", int: "number", float: "number", bool: "boolean"}


class EventParseError(ValueError):
    """A line could not be turned into an event. `reason` is human-readable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SourcedEvent:
    """An accepted event plus the file line it came from (kept outside the event)."""

    line_number: int
    event: Event


@dataclass(frozen=True)
class RejectedLine:
    line_number: int
    reason: str
    raw: str


@dataclass(frozen=True)
class LoadResult:
    accepted: list[SourcedEvent]
    rejected: list[RejectedLine]


def parse_event(line: str) -> Event:
    """Validate one JSONL line. Raises EventParseError with a readable reason."""
    if not line.strip():
        raise EventParseError("blank line")

    # Pre-check the JSON ourselves so broken or non-object lines get clear reasons.
    try:
        data = json.loads(line)
    except json.JSONDecodeError as err:
        raise EventParseError(f"invalid JSON: {err.msg} at column {err.colno}") from err
    if not isinstance(data, dict):
        found = _JSON_TYPE_NAMES.get(type(data), "null")
        raise EventParseError(f"expected a JSON object, got {found}")

    # Validate from the JSON text, not `data`: strict mode only accepts ISO
    # timestamp strings when validating JSON input.
    try:
        return _EVENT_ADAPTER.validate_json(line)
    except ValidationError as err:
        raise EventParseError(_describe(err)) from err


def _describe(err: ValidationError) -> str:
    """Turn Pydantic errors into 'field: message' parts, one per problem."""
    parts = []
    for error in err.errors():
        # loc is a tuple like ("queue_snapshot", "tickets_waiting"). It is empty
        # when the `type` discriminator itself is missing or unknown.
        field = ".".join(str(part) for part in error["loc"]) or "type"
        parts.append(f"{field}: {error['msg']}")
    return "; ".join(parts)


def load_events(path: Path) -> LoadResult:
    """Parse every line of a JSONL file, keeping file order and line numbers."""
    accepted: list[SourcedEvent] = []
    rejected: list[RejectedLine] = []
    with path.open(encoding="utf-8") as file:
        for line_number, text in enumerate(file, start=1):
            raw = text.rstrip("\n")
            try:
                event = parse_event(raw)
            except EventParseError as err:
                rejected.append(RejectedLine(line_number, err.reason, raw))
            else:
                accepted.append(SourcedEvent(line_number, event))
    return LoadResult(accepted, rejected)
