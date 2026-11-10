"""Review fix: optional backlog recovery threshold (`resolve_at_or_below`).

Blank (None) keeps the PLAN §4 behaviour: a backlog incident resolves as soon
as tickets_waiting is at or below warning_above. A set value adds a hold band
above it, like the SLA rule's resolve_ratio: inside the band an open incident
stays open (a critical one drops silently to warning) and nothing new opens.
The seeded rules never set it, so the oracle is unchanged.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from alerts.engine import Engine
from alerts.incidents import Notification
from alerts.ingest import SourcedEvent, load_events
from alerts.presentation import rule_preview
from alerts.routing import seeded_rules
from alerts.rules import BacklogRule, Decision, Rule, evaluate
from alerts.storage import Store
from tests.builders import at, snapshot
from tests.engine_builders import Driver, brief, edited, hm, seeded
from tests.test_dataset_oracle import ORACLE

BACKLOG = "backlog.lead_billing"
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"
EVENTS: list[SourcedEvent] = load_events(DATA_PATH).accepted


def backlog_rule(**overrides: Any) -> BacklogRule:
    """A valid backlog rule (warning > 15, critical > 20) plus overrides."""
    return BacklogRule(**{"rule_id": "r", "scope": "all_owned", "recipient": "lead_billing", **overrides})


def decide(rule: BacklogRule, tickets: int) -> str:
    return evaluate(rule, snapshot("10:00", tickets_waiting=tickets), at("10:00")).condition


def with_recovery(value: int | None) -> list[Rule]:
    """The seeded rules with backlog.lead_billing's recovery threshold set to `value`."""
    return [edited(BACKLOG, resolve_at_or_below=value) if r.rule_id == BACKLOG else r for r in seeded_rules()]


def billing(ts: str, tickets: int) -> object:
    return snapshot(ts, "billing", tickets_waiting=tickets)


# ---------------------------------------------------------------------------
# Config and pure evaluation
# ---------------------------------------------------------------------------


def test_blank_is_the_default_and_keeps_current_behaviour() -> None:
    rule = backlog_rule()
    assert rule.resolve_at_or_below is None
    assert [decide(rule, n) for n in (15, 16, 20, 21)] == ["clear", "warning", "warning", "critical"]
    assert [r.resolve_at_or_below for r in seeded_rules() if isinstance(r, BacklogRule)] == [None, None]


@pytest.mark.parametrize(
    ("tickets", "expected"),
    [(0, "clear"), (12, "clear"), (13, "hold"), (15, "hold"), (16, "warning"), (20, "warning"), (21, "critical")],
)
def test_configured_threshold_boundaries(tickets: int, expected: str) -> None:
    assert decide(backlog_rule(resolve_at_or_below=12), tickets) == expected


def test_threshold_equal_to_warning_behaves_like_blank() -> None:
    rule = backlog_rule(resolve_at_or_below=15)
    assert [decide(rule, n) for n in (15, 16)] == ["clear", "warning"]
    assert evaluate(rule, snapshot("10:00", tickets_waiting=14), at("10:00")) == Decision("clear")


@pytest.mark.parametrize("value", [None, 0, 12, 15])
def test_valid_thresholds_accepted(value: int | None) -> None:
    assert backlog_rule(resolve_at_or_below=value).resolve_at_or_below == value


@pytest.mark.parametrize(
    ("value", "error_type"),
    [(-1, "greater_than_equal"), (12.5, "int_type"), (True, "int_type"), ("12", "int_type")],
    ids=["negative", "fraction", "bool", "string-number"],
)
def test_invalid_threshold_types_rejected(value: Any, error_type: str) -> None:
    with pytest.raises(ValidationError) as caught:
        backlog_rule(resolve_at_or_below=value)
    assert [(e["loc"][0], e["type"]) for e in caught.value.errors()] == [("resolve_at_or_below", error_type)]


def test_threshold_above_warning_rejected() -> None:
    with pytest.raises(ValidationError, match="resolve_at_or_below must be at most warning_above"):
        backlog_rule(resolve_at_or_below=16)
    with pytest.raises(ValidationError, match="resolve_at_or_below must be at most warning_above"):
        backlog_rule(warning_above=10, critical_above=20, resolve_at_or_below=12)


def test_preview_states_the_threshold_only_when_set() -> None:
    blank = ("Notify lead_billing when any queue they own (billing, vip) "
             "has more than 15 tickets waiting; critical above 20.")  # fmt: skip
    assert rule_preview(seeded(BACKLOG)) == blank
    assert rule_preview(edited(BACKLOG, resolve_at_or_below=12)) == (
        blank.removesuffix(".") + "; recovers at or below 12."
    )


# ---------------------------------------------------------------------------
# Engine behaviour
# ---------------------------------------------------------------------------


def test_threshold_stops_flapping() -> None:
    driver = Driver(with_recovery(12))
    values = [("09:00", 16), ("09:01", 15), ("09:02", 16), ("09:03", 14), ("09:04", 16), ("09:05", 15), ("09:06", 12)]
    emitted = driver.send(*(billing(ts, n) for ts, n in values))
    assert brief(emitted) == [
        (BACKLOG, "billing", "opened", "warning", "09:00", "09:00"),
        (BACKLOG, "billing", "resolved", "warning", "09:06", "09:06"),
    ]


def test_value_inside_the_band_never_opens_an_incident() -> None:
    driver = Driver(with_recovery(12))
    assert driver.send(billing("09:00", 13), billing("09:01", 15), billing("09:02", 14)) == []
    assert driver.engine.incidents == {}

    driver.send(billing("09:03", 16), billing("09:04", 12))  # opened, then resolved
    assert driver.send(billing("09:05", 15), billing("09:06", 13)) == []  # the band does not reopen it
    assert [i.status for i in driver.engine.incidents.values()] == ["resolved"]


def test_critical_drops_silently_to_warning_in_the_band() -> None:
    driver = Driver(with_recovery(12))
    emitted: list[Notification] = []
    severities: list[str | None] = []
    for ts, n in [("09:00", 21), ("09:01", 14), ("09:02", 21), ("09:03", 12)]:
        emitted += driver.send(billing(ts, n))
        severities.append(driver.engine.incidents[f"{BACKLOG}:billing:1"].severity)
    assert brief(emitted) == [
        (BACKLOG, "billing", "opened", "critical", "09:00", "09:00"),
        (BACKLOG, "billing", "resolved", "critical", "09:03", "09:03"),  # keeps its last severity
    ]
    assert severities == ["critical", "warning", "critical", "critical"]


def test_clearing_the_threshold_while_held_resolves_at_the_next_snapshot() -> None:
    driver = Driver(with_recovery(12))
    driver.send(billing("09:00", 16), billing("09:01", 14))  # opened, then held
    assert driver.engine.update_rule(seeded(BACKLOG)) == []  # an edit never notifies
    assert brief(driver.send(billing("09:02", 14))) == [
        (BACKLOG, "billing", "resolved", "warning", "09:02", "09:02"),
    ]


# ---------------------------------------------------------------------------
# Backward compatibility: configs stored before the field existed
# ---------------------------------------------------------------------------


def test_stored_config_without_the_field_loads_as_blank() -> None:
    old = {k: v for k, v in seeded(BACKLOG).model_dump(mode="json").items() if k != "resolve_at_or_below"}
    assert BacklogRule.model_validate_json(json.dumps(old)).resolve_at_or_below is None


def test_store_recovers_old_backlog_configs_without_the_field(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    store = Store.create(path, seeded_rules())
    for sourced in EVENTS[:40]:
        store.step(sourced)
    store.update_rule(edited(BACKLOG, warning_above=14))
    for sourced in EVENTS[40:70]:
        store.step(sourced)
    notifications = store.stored_notifications()
    store.close()

    # Rewrite every stored backlog config as it looked before the field existed.
    conn = sqlite3.connect(path)
    with conn:
        for table, column, key in (("baseline_rules", "config", "rule_id"), ("journal", "payload", "seq")):
            rows = conn.execute(f"SELECT {key}, {column} FROM {table} WHERE template = 'backlog'").fetchall()
            for row_key, text in rows:
                config = json.loads(text)
                config.pop("resolve_at_or_below", None)
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (json.dumps(config), row_key))
    conn.close()

    store = Store.open(path)  # replays the rewritten journal and checks it against the stored outputs
    assert store.stored_notifications() == notifications
    backlog = [r for r in store.engine.rules.values() if isinstance(r, BacklogRule)]
    assert [(r.rule_id, r.warning_above, r.resolve_at_or_below) for r in backlog] == [
        ("backlog.lead_billing", 14, None),
        ("backlog.lead_support", 15, None),
    ]
    store.close()


# ---------------------------------------------------------------------------
# The real dataset
# ---------------------------------------------------------------------------


def oracle_rows(engine: Engine) -> list[tuple[Any, ...]]:
    return [
        (hm(n.emitted_at), hm(n.effective_at), n.rule_id, n.subject_id, n.kind, n.severity, n.recipient)
        for n in engine.notifications
    ]


def test_dataset_with_blank_thresholds_matches_oracle() -> None:
    engine = Engine(with_recovery(None))
    engine.run_all(EVENTS)
    assert oracle_rows(engine) == ORACLE


def test_dataset_with_threshold_12_moves_billing_backlog_recovery_to_1015() -> None:
    # Billing backlog: 14 at 10:00 (line 68) is inside the band, 8 at 10:15 (line 77) clears it.
    # backlog.lead_billing runs before sla.lead_billing in the same 10:15 step (rule_id order).
    engine = Engine(with_recovery(12))
    engine.run_all(EVENTS)
    old_row = ("10:00", "10:00", BACKLOG, "billing", "resolved", "warning", "lead_billing")
    new_row = ("10:15", "10:15", BACKLOG, "billing", "resolved", "warning", "lead_billing")
    expected = [row for row in ORACLE if row != old_row]
    expected.insert(expected.index(("10:15", "10:15", "sla.lead_billing", "billing", "resolved", "critical",
                                    "lead_billing")), new_row)  # fmt: skip
    assert oracle_rows(engine) == expected
    assert (len(engine.notifications), len(engine.active_incidents())) == (20, 4)
