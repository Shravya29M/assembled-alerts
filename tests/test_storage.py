"""Checkpoint 3.3 — SQLite journal, recovery and rollback (Decisions 48-50).

The recovery scenario mixes events with an edit, a disable and a re-enable,
so replaying with only the latest rules would give a different result.
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from alerts.engine import Engine, ReplayFinishedError
from alerts.ingest import SourcedEvent, load_events
from alerts.routing import RoutingError, seeded_rules
from alerts.rules import Rule
from alerts.storage import RecoveryMismatchError, StorageError, Store
from tests.builders import at
from tests.engine_builders import edited, engine_snapshot
from tests.test_dataset_oracle import ORACLE
from tests.test_rule_creation import (
    R1,
    R2,
    R2_NEW_ROWS,
    R3,
    R4,
    R4_NEW_ROWS,
    R4_TOTAL,
    oracle_shape,
    split,
)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"
EVENTS: list[SourcedEvent] = load_events(DATA_PATH).accepted

Action = tuple[str, Any]


def scenario() -> list[Action]:
    actions: list[Action] = []
    for sourced in EVENTS:
        actions.append(("event", sourced))
        if sourced.line_number == 50:
            actions.append(("update", edited("long_call.lead_support", min_duration_sec=3600)))
        if sourced.line_number == 60:
            actions.append(("disable", "sla.lead_billing"))
        if sourced.line_number == 80:
            actions.append(("enable", "sla.lead_billing"))
    actions.append(("finish", None))
    return actions


def plain_run() -> list[Action]:
    return [("event", sourced) for sourced in EVENTS] + [("finish", None)]


def perform(target: Engine | Store, actions: list[Action]) -> None:
    for kind, arg in actions:
        if kind == "event":
            target.step(arg)
        elif kind == "update":
            target.update_rule(arg)
        elif kind == "disable":
            target.disable_rule(arg)
        elif kind == "enable":
            target.enable_rule(arg)
        elif kind == "add":
            target.add_rule(arg)
        else:
            target.finish()


def reference(actions: list[Action]) -> Engine:
    engine = Engine(seeded_rules())
    perform(engine, actions)
    return engine


# ---------------------------------------------------------------------------
# Journal and stored outputs
# ---------------------------------------------------------------------------


def test_journal_and_outputs_after_plain_run(tmp_path: Path) -> None:
    store = Store.create(tmp_path / "alerts.db", seeded_rules())
    perform(store, plain_run())
    events = [row for row in store.journal() if row.kind == "event"]
    assert [row.line_number for row in events] == list(range(1, 97))
    assert Counter(row.outcome for row in events) == {"accepted": 94, "duplicate": 1, "stale": 1}
    assert store.stored_notifications() == store.engine.notifications
    assert len(store.stored_notifications()) == 20
    assert store.stored_incidents() == list(store.engine.incidents.values())


def test_rule_change_is_journaled_in_position(tmp_path: Path) -> None:
    store = Store.create(tmp_path / "alerts.db", seeded_rules())
    perform(store, scenario())
    kinds = [(row.kind, row.line_number, row.rule_id, row.rule_version) for row in store.journal()]
    update = kinds.index(("rule_change", None, "long_call.lead_support", 2))
    assert [k for k in kinds[:update] if k[0] == "event"][-1][1] == 50
    assert kinds[update + 1][:2] == ("event", 51)


# ---------------------------------------------------------------------------
# Deterministic recovery
# ---------------------------------------------------------------------------


def test_recovery_reproduces_everything(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    original = Store.create(path, seeded_rules())
    perform(original, scenario())
    expected = engine_snapshot(original.engine)
    original.close()

    recovered = Store.open(path)
    assert engine_snapshot(recovered.engine) == expected  # IDs, lifecycle, order, versions, timers
    assert engine_snapshot(recovered.engine) == engine_snapshot(reference(scenario()))


def test_recover_mid_run_then_continue(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    actions = scenario()
    split = next(i for i, (kind, arg) in enumerate(actions) if kind == "event" and arg.line_number == 70) + 1

    first = Store.create(path, seeded_rules())
    perform(first, actions[:split])
    first.close()

    resumed = Store.open(path)
    perform(resumed, actions[split:])
    assert engine_snapshot(resumed.engine) == engine_snapshot(reference(actions))
    assert resumed.stored_notifications() == resumed.engine.notifications


def test_tampered_outputs_fail_recovery_self_check(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    store = Store.create(path, seeded_rules())
    perform(store, plain_run())
    store.close()

    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM notifications WHERE seq = (SELECT MAX(seq) FROM notifications)")

    with pytest.raises(RecoveryMismatchError):
        Store.open(path)


# ---------------------------------------------------------------------------
# Failed write: rollback and rebuild
# ---------------------------------------------------------------------------


class FailingConnection:
    """Wraps a real connection; while armed, any INSERT into `table` fails."""

    def __init__(self, conn: sqlite3.Connection, table: str) -> None:
        self._conn = conn
        self._prefix = f"INSERT INTO {table}".upper()
        self.armed = False

    def _check(self, sql: str) -> None:
        if self.armed and sql.lstrip().upper().startswith(self._prefix):
            raise sqlite3.OperationalError("injected failure")

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        self._check(sql)
        return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, seq: Any) -> sqlite3.Cursor:
        self._check(sql)
        return self._conn.executemany(sql, seq)

    def __enter__(self) -> "FailingConnection":
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._conn.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def failing_connect(holder: list[FailingConnection]) -> Callable[[str], Any]:
    def connect(path: str) -> FailingConnection:
        holder.append(FailingConnection(sqlite3.connect(path), "notifications"))
        return holder[-1]

    return connect


def test_failed_write_rolls_back_and_rebuilds(tmp_path: Path) -> None:
    connections: list[FailingConnection] = []
    store = Store.create(tmp_path / "alerts.db", seeded_rules(), connect=failing_connect(connections))
    line_39 = next(i for i, s in enumerate(EVENTS) if s.line_number == 39)  # emits two notifications
    for sourced in EVENTS[:line_39]:
        store.step(sourced)

    before_engine = engine_snapshot(store.engine)
    before_journal = store.journal()
    before_notifications = store.stored_notifications()
    before_incidents = store.stored_incidents()

    connections[-1].armed = True
    with pytest.raises(StorageError):
        store.step(EVENTS[line_39])
    connections[-1].armed = False

    assert store.journal() == before_journal  # journal row rolled back too
    assert store.stored_notifications() == before_notifications
    assert store.stored_incidents() == before_incidents  # line 39's two new incidents rolled back
    assert engine_snapshot(store.engine) == before_engine  # memory rebuilt from journal

    for sourced in EVENTS[line_39:]:
        store.step(sourced)
    store.finish()
    assert engine_snapshot(store.engine) == engine_snapshot(reference(plain_run()))


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_everything_and_rerun_is_identical(tmp_path: Path) -> None:
    store = Store.create(tmp_path / "alerts.db", seeded_rules())
    perform(store, plain_run())
    first = store.stored_notifications()

    store.reset()
    assert store.journal() == []
    assert store.stored_notifications() == []
    assert store.engine.notifications == []

    perform(store, plain_run())
    assert store.stored_notifications() == first


class DeadConnection(FailingConnection):
    """While armed, every statement fails: the write and the rebuild that follows it."""

    def _check(self, sql: str) -> None:
        if self.armed:
            raise sqlite3.OperationalError("injected failure")


def test_failed_rebuild_makes_store_unusable_until_reopened(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    connections: list[FailingConnection] = []

    def connect(target: str) -> FailingConnection:
        connections.append(DeadConnection(sqlite3.connect(target), "notifications"))
        return connections[-1]

    store = Store.create(path, seeded_rules(), connect=connect)
    for sourced in EVENTS[:10]:
        store.step(sourced)

    connections[-1].armed = True
    with pytest.raises(StorageError):
        store.step(EVENTS[10])
    connections[-1].armed = False
    with pytest.raises(StorageError, match="reopen"):
        store.step(EVENTS[10])  # still refused: memory may be inconsistent
    store.close()

    reopened = Store.open(path)
    assert [row.line_number for row in reopened.journal()] == list(range(1, 11))
    assert engine_snapshot(reopened.engine) == engine_snapshot(reference([("event", s) for s in EVENTS[:10]]))


# ---------------------------------------------------------------------------
# Checkpoint 3.2 behaviour under persistence
# ---------------------------------------------------------------------------


def test_finish_and_post_finish_edit_survive_recovery(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    store = Store.create(path, seeded_rules())
    perform(store, plain_run())
    store.update_rule(edited("long_call.lead_support", min_duration_sec=3600))  # after finish
    assert store.engine.pending_timers()[("long_call.lead_support", "a_42")] == at("10:48")  # moved
    expected = engine_snapshot(store.engine)
    store.close()

    recovered = Store.open(path)
    assert engine_snapshot(recovered.engine) == expected
    assert recovered.engine.finished
    assert [row.kind for row in recovered.journal()][-2:] == ["finish", "rule_change"]
    with pytest.raises(ReplayFinishedError):
        recovered.step(EVENTS[0])


def test_repeated_finish_is_journaled_once(tmp_path: Path) -> None:
    store = Store.create(tmp_path / "alerts.db", seeded_rules())
    perform(store, plain_run())
    assert store.finish() == []
    assert [row.kind for row in store.journal()].count("finish") == 1


def test_rejected_operations_are_not_journaled(tmp_path: Path) -> None:
    store = Store.create(tmp_path / "alerts.db", seeded_rules())
    perform(store, plain_run())

    def observed() -> tuple[Any, ...]:
        return (store.journal(), store.stored_notifications(), store.stored_incidents(),
                engine_snapshot(store.engine))  # fmt: skip

    before = observed()
    with pytest.raises(RoutingError):
        store.update_rule(edited("backlog.lead_billing", recipient="lead_x"))
    with pytest.raises(ReplayFinishedError):
        store.step(EVENTS[0])
    assert observed() == before


def test_reset_keeps_rules_and_versions_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    edit = edited("long_call.lead_support", min_duration_sec=3600)
    store = Store.create(path, seeded_rules())
    perform(store, plain_run())
    store.update_rule(edit)
    store.reset()
    store.close()

    reopened = Store.open(path)
    assert reopened.engine.rules["long_call.lead_support"] == edit
    assert reopened.engine.rule_version("long_call.lead_support") == 2  # not restarted at 1
    perform(reopened, plain_run())

    expected = Engine(seeded_rules())
    expected.update_rule(edit)
    perform(expected, plain_run())
    assert engine_snapshot(reopened.engine) == engine_snapshot(expected)


def test_tampered_incident_fails_recovery_self_check(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    store = Store.create(path, seeded_rules())
    perform(store, plain_run())
    store.close()

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE incidents SET status = 'resolved' WHERE incident_id = 'long_call.lead_support:a_23:1'")

    with pytest.raises(RecoveryMismatchError):
        Store.open(path)



# ---------------------------------------------------------------------------
# Rebuild only when the engine may have changed (pre-mutation errors re-raise as-is)
# ---------------------------------------------------------------------------


def count_recoveries(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Wrap alerts.storage._recover so a test can see whether a rebuild ran."""
    import alerts.storage

    calls: list[int] = []
    original = alerts.storage._recover

    def counting(conn: Any) -> Engine:
        calls.append(1)
        return original(conn)

    monkeypatch.setattr(alerts.storage, "_recover", counting)
    return calls


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        (lambda store: store.update_rule(edited("backlog.lead_billing", recipient="lead_x")), RoutingError),
        (lambda store: store.update_rule(
            seeded_rules()[0].model_copy(update={"warning_above": 25})), ValueError),  # ValidationError
        (lambda store: store.disable_rule("no_such_rule"), ValueError),
        (lambda store: store.step(EVENTS[0]), ReplayFinishedError),
    ],
    ids=["bad-routing", "invalid-config", "unknown-rule", "step-after-finish"],
)
def test_pre_mutation_errors_do_not_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: Callable[[Store], Any], error: type[Exception]
) -> None:
    store = Store.create(tmp_path / "alerts.db", seeded_rules())
    perform(store, plain_run())
    before = (store.journal(), engine_snapshot(store.engine))
    recoveries = count_recoveries(monkeypatch)
    with pytest.raises(error):
        operation(store)
    assert recoveries == []
    assert (store.journal(), engine_snapshot(store.engine)) == before


def test_error_after_engine_mutation_rebuilds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store.create(tmp_path / "alerts.db", seeded_rules())
    for sourced in EVENTS[:5]:
        store.step(sourced)
    before = (store.journal(), engine_snapshot(store.engine))

    # Fail once, inside rule evaluation: the event has already been applied
    # to the replay state (clock, freshness, dedupe) when this raises.
    original = Engine._evaluate
    failed: list[int] = []

    def fail_once(self: Engine, *args: Any) -> None:
        if not failed:
            failed.append(1)
            raise RuntimeError("bug after mutation")
        original(self, *args)

    monkeypatch.setattr(Engine, "_evaluate", fail_once)
    recoveries = count_recoveries(monkeypatch)
    with pytest.raises(RuntimeError, match="bug after mutation"):
        store.step(EVENTS[5])
    assert recoveries == [1]
    assert (store.journal(), engine_snapshot(store.engine)) == before

    for sourced in EVENTS[5:]:
        store.step(sourced)
    store.finish()
    assert engine_snapshot(store.engine) == engine_snapshot(reference(plain_run()))


# ---------------------------------------------------------------------------
# Phase 5.3 — created rules are journaled, recovered and kept across Reset
# ---------------------------------------------------------------------------


def with_rule_added_after(line: int, rule: Rule) -> list[Action]:
    actions: list[Action] = []
    for sourced in EVENTS:
        actions.append(("event", sourced))
        if sourced.line_number == line:
            actions.append(("add", rule))
    actions.append(("finish", None))
    return actions


def index_after_line(actions: list[Action], line: int) -> int:
    return next(i for i, (kind, arg) in enumerate(actions) if kind == "event" and arg.line_number == line) + 1


def test_rule_add_is_journaled_in_position(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    store = Store.create(path, seeded_rules())
    perform(store, with_rule_added_after(50, R2))
    journal = store.journal()
    added = [i for i, row in enumerate(journal) if row.kind == "rule_add"]
    assert len(added) == 1
    row = journal[added[0]]
    assert (row.rule_id, row.rule_version, row.line_number) == ("adherence.lead_billing.2", 1, None)
    assert json.loads(row.payload or "") == R2.model_dump(mode="json")
    assert (journal[added[0] - 1].kind, journal[added[0] - 1].line_number) == ("event", 50)
    assert (journal[added[0] + 1].kind, journal[added[0] + 1].line_number) == ("event", 51)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT template FROM journal WHERE kind = 'rule_add'").fetchall() == [("adherence",)]


def test_recovery_replays_added_rule_mid_run(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    actions = with_rule_added_after(50, R2)
    split_at = index_after_line(actions, 60)

    first = Store.create(path, seeded_rules())
    perform(first, actions[:split_at])
    expected = engine_snapshot(first.engine)  # includes R2's timer and version
    first.close()

    resumed = Store.open(path)
    assert engine_snapshot(resumed.engine) == expected
    perform(resumed, actions[split_at:])
    assert engine_snapshot(resumed.engine) == engine_snapshot(reference(actions))
    new, others = split(resumed.stored_notifications(), R2.rule_id)
    assert (new, others) == (R2_NEW_ROWS, ORACLE)


def test_added_then_edited_rule_survives_recovery(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    edit = R3.model_copy(update={"min_duration_sec": 3600})
    actions: list[Action] = [("event", s) for s in EVENTS if s.line_number <= 30]
    actions += [("add", R3), ("update", edit)]
    store = Store.create(path, seeded_rules())
    perform(store, actions)
    expected = engine_snapshot(store.engine)
    store.close()

    recovered = Store.open(path)
    assert recovered.engine.rules[R3.rule_id] == edit
    assert recovered.engine.rule_version(R3.rule_id) == 2
    assert engine_snapshot(recovered.engine) == expected


def test_rule_added_after_finish_survives_restart_and_reset(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    store = Store.create(path, seeded_rules())
    perform(store, plain_run())
    outputs = (store.stored_notifications(), store.stored_incidents())
    store.add_rule(R4)
    store.close()

    reopened = Store.open(path)  # 1. still finished, rule kept, nothing new
    assert reopened.engine.finished
    assert (reopened.engine.rules[R4.rule_id], reopened.engine.rule_version(R4.rule_id)) == (R4, 1)
    assert (reopened.stored_notifications(), reopened.stored_incidents()) == outputs
    reopened.reset()
    reopened.close()

    after_reset = Store.open(path)  # 2. the baseline holds the new rule at version 1
    assert after_reset.journal() == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM baseline_rules WHERE rule_id = ?", (R4.rule_id,)).fetchall() == [(1,)]
    perform(after_reset, plain_run())  # 3. it runs on the next replay
    new, others = split(after_reset.stored_notifications(), R4.rule_id)
    assert (new, others) == (R4_NEW_ROWS, ORACLE)
    assert len(after_reset.stored_notifications()) == R4_TOTAL
    expected = engine_snapshot(after_reset.engine)
    after_reset.close()

    assert engine_snapshot(Store.open(path).engine) == expected  # 4. and survives another restart


@pytest.mark.parametrize(
    ("rule", "error", "match"),
    [
        (edited("backlog.lead_billing"), ValueError, "duplicate"),  # rule_id already exists
        (R1.model_copy(update={"recipient": "lead_x"}), RoutingError, None),
    ],
    ids=["duplicate-rule-id", "bad-routing"],
)
def test_rejected_add_is_not_journaled_and_not_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rule: Rule, error: type[Exception], match: str | None
) -> None:
    store = Store.create(tmp_path / "alerts.db", seeded_rules())
    for sourced in EVENTS[:50]:
        store.step(sourced)
    before = (store.journal(), store.stored_notifications(), store.stored_incidents(), engine_snapshot(store.engine))
    recoveries = count_recoveries(monkeypatch)
    with pytest.raises(error, match=match):
        store.add_rule(rule)
    assert recoveries == []
    assert (store.journal(), store.stored_notifications(), store.stored_incidents(),
            engine_snapshot(store.engine)) == before  # fmt: skip


def test_failed_write_during_add_rolls_back(tmp_path: Path) -> None:
    connections: list[FailingConnection] = []

    def connect(target: str) -> FailingConnection:  # adding a rule writes only a journal row
        connections.append(FailingConnection(sqlite3.connect(target), "journal"))
        return connections[-1]

    store = Store.create(tmp_path / "alerts.db", seeded_rules(), connect=connect)
    for sourced in EVENTS[:50]:
        store.step(sourced)
    before = (store.journal(), engine_snapshot(store.engine))

    connections[-1].armed = True
    with pytest.raises(StorageError):
        store.add_rule(R2)
    connections[-1].armed = False

    assert (store.journal(), engine_snapshot(store.engine)) == before
    assert R2.rule_id not in store.engine.rules
    store.add_rule(R2)
    assert [row.kind for row in store.journal()].count("rule_add") == 1


def test_seeded_store_without_additions_is_unchanged(tmp_path: Path) -> None:
    store = Store.create(tmp_path / "alerts.db", seeded_rules())
    perform(store, plain_run())
    assert {row.kind for row in store.journal()} == {"event", "finish"}
    assert oracle_shape(store.stored_notifications()) == ORACLE
