"""SQLite persistence: a journal of inputs (append-only within the current replay) plus stored outputs.

The engine is deterministic, so its whole state follows from its inputs
(Decision 49). The database keeps:

- baseline_rules: rule configs and versions at the start of the current replay
- journal: every input in order (events with their outcome, rule changes, finish)
- incidents, notifications: the outputs, so recovery can check itself

Each operation has two parts:
  1. Pre-checks with no side effects (step after finish, rule validation).
     Their errors are re-raised as-is: nothing has changed.
  2. The engine mutation, then one transaction that writes it. If either
     fails, the transaction rolls back and the engine is rebuilt from the
     database (Decision 50). If that rebuild also fails, the Store refuses
     further use until it is reopened.

Recovery (Store.open) replays the journal from the baseline and checks the
result against the stored outputs. It reads no wall clock and delivers nothing.
"""

import copy
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from alerts.engine import Engine, ReplayFinishedError, StepResult
from alerts.incidents import Incident, Notification
from alerts.ingest import SourcedEvent, parse_event
from alerts.rules import AdherenceRule, BacklogRule, LongCallRule, Rule, SlaRule

Connect = Callable[[str], Any]  # sqlite3.connect, or a test wrapper with the same interface

_TEMPLATES: dict[str, type[Rule]] = {
    "backlog": BacklogRule,
    "sla": SlaRule,
    "adherence": AdherenceRule,
    "long_call": LongCallRule,
}
_TEMPLATE_OF = {cls: name for name, cls in _TEMPLATES.items()}

_SCHEMA = [
    """CREATE TABLE baseline_rules (
        rule_id TEXT PRIMARY KEY, template TEXT NOT NULL, config TEXT NOT NULL, version INTEGER NOT NULL)""",
    """CREATE TABLE journal (
        seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, line_number INTEGER, event_id TEXT,
        payload TEXT, outcome TEXT, rule_id TEXT, template TEXT, rule_version INTEGER)""",
    """CREATE TABLE incidents (
        incident_id TEXT PRIMARY KEY, position INTEGER NOT NULL UNIQUE, rule_id TEXT NOT NULL,
        subject_id TEXT NOT NULL, recipient TEXT NOT NULL, status TEXT NOT NULL, severity TEXT,
        opened_at TEXT NOT NULL, closed_at TEXT)""",
    """CREATE TABLE notifications (
        seq INTEGER PRIMARY KEY, incident_id TEXT NOT NULL, rule_id TEXT NOT NULL,
        rule_version INTEGER NOT NULL, subject_id TEXT NOT NULL, recipient TEXT NOT NULL,
        kind TEXT NOT NULL, severity TEXT, effective_at TEXT NOT NULL, emitted_at TEXT NOT NULL,
        line_number INTEGER)""",
]

_INCIDENT_COLUMNS = "incident_id, rule_id, subject_id, recipient, status, severity, opened_at, closed_at"
_NOTIFICATION_COLUMNS = ("seq, incident_id, rule_id, rule_version, subject_id, recipient, kind, severity, "
                         "effective_at, emitted_at, line_number")  # fmt: skip


class StorageError(RuntimeError):
    """A database operation failed, or the store must be reopened."""


class RecoveryMismatchError(StorageError):
    """Replaying the journal did not reproduce the stored records."""


@dataclass(frozen=True)
class JournalRow:
    seq: int
    kind: str  # event | rule_change | finish
    line_number: int | None
    event_id: str | None
    outcome: str | None
    rule_id: str | None
    rule_version: int | None


@dataclass(frozen=True)
class _Entry:
    """A journal row about to be written (seq is assigned by SQLite)."""

    kind: str
    line_number: int | None = None
    event_id: str | None = None
    payload: str | None = None
    outcome: str | None = None
    rule_id: str | None = None
    template: str | None = None
    rule_version: int | None = None


class Store:
    """The supported way to change replay state: every mutation is journaled."""

    def __init__(self, conn: Any, engine: Engine) -> None:  # use create() or open()
        self._conn = conn
        self._engine: Engine | None = engine
        self._unusable = ""  # why the store refuses use; "" while usable

    # -- construction -------------------------------------------------------

    @classmethod
    def create(cls, path: Path, rules: list[Rule], *, connect: Connect = sqlite3.connect) -> "Store":
        """A new, empty store whose baseline is `rules` at version 1.

        Known limitation: if creating the tables fails, the transaction rolls back but
        SQLite leaves an empty file behind, and a later create() refuses that path.
        """
        engine = Engine(rules)  # validates rules before any file is created
        if Path(path).exists():
            raise StorageError(f"{path} already exists; use Store.open")
        conn = connect(str(path))
        with conn:
            conn.execute("BEGIN")
            for statement in _SCHEMA:
                conn.execute(statement)
            _write_baseline(conn, engine)
        return cls(conn, engine)

    @classmethod
    def open(cls, path: Path, *, connect: Connect = sqlite3.connect) -> "Store":
        """Recover by replaying the journal; raises RecoveryMismatchError if outputs differ."""
        if not Path(path).exists():
            raise StorageError(f"{path} does not exist")
        conn = connect(str(path))
        try:
            return cls(conn, _recover(conn))
        except sqlite3.Error as err:
            conn.close()
            raise StorageError(f"{path} is not a readable alerts store: {err}") from err
        except BaseException:
            conn.close()
            raise

    def close(self) -> None:
        self._conn.close()
        self._engine = None
        self._unusable = "the store is closed; reopen it with Store.open"

    # -- inspection (read-only) ---------------------------------------------

    @property
    def engine(self) -> Engine:
        """A deep-copied snapshot of the engine, for inspection only.

        Changes to the copy are never persisted. Every persistent change must go
        through Store methods (step, finish, run_all, rule methods, reset).
        """
        return copy.deepcopy(self._live())

    def journal(self) -> list[JournalRow]:
        rows = self._conn.execute(
            "SELECT seq, kind, line_number, event_id, outcome, rule_id, rule_version FROM journal ORDER BY seq"
        ).fetchall()
        return [JournalRow(*row) for row in rows]

    def stored_notifications(self) -> list[Notification]:
        return _load_notifications(self._conn)

    def stored_incidents(self) -> list[Incident]:
        return _load_incidents(self._conn)

    # -- mutations ----------------------------------------------------------

    def step(self, sourced: SourcedEvent) -> StepResult:
        if self._live().finished:  # pre-check: nothing changes, so no rebuild
            raise ReplayFinishedError("the replay is finished; call reset() before stepping again")
        before = self._mark()
        result: StepResult = self._apply(lambda engine: engine.step(sourced))
        event = sourced.event
        self._persist(before, _Entry("event", sourced.line_number, event.event_id, event.model_dump_json(),
                                     result.outcome))  # fmt: skip
        return result

    def finish(self) -> list[Notification]:
        if self._live().finished:
            return []  # repeated finish(): a no-op, not journaled
        before = self._mark()
        emitted: list[Notification] = self._apply(lambda engine: engine.finish())
        self._persist(before, _Entry("finish"))
        return emitted

    def run_all(self, events: Iterable[SourcedEvent]) -> None:
        for sourced in events:
            self.step(sourced)
        self.finish()

    def update_rule(self, rule: Rule) -> list[Notification]:
        return self._change_rule(self._live().validate_rule(rule))  # pre-check: no side effects

    def disable_rule(self, rule_id: str) -> list[Notification]:
        return self._change_rule(self._flipped(rule_id, enabled=False))

    def enable_rule(self, rule_id: str) -> list[Notification]:
        return self._change_rule(self._flipped(rule_id, enabled=True))

    def reset(self) -> None:
        """Clear the journal and outputs; the current rules and versions become the new baseline."""
        engine = self._live()
        try:
            with self._conn:
                self._conn.execute("BEGIN")
                for table in ("journal", "incidents", "notifications", "baseline_rules"):
                    self._conn.execute(f"DELETE FROM {table}")
                _write_baseline(self._conn, engine)
        except sqlite3.Error as err:  # rolled back; memory was not touched yet
            raise StorageError(f"reset failed and was rolled back: {err}") from err
        engine.reset()

    # -- internals ----------------------------------------------------------

    def _live(self) -> Engine:
        if self._engine is None:
            raise StorageError(self._unusable)
        return self._engine

    def _flipped(self, rule_id: str, *, enabled: bool) -> Rule:
        """Pre-check for disable/enable: the current rule with `enabled` set, fully validated."""
        engine = self._live()
        if rule_id not in engine.rules:
            raise ValueError(f"unknown rule_id {rule_id!r}")
        return engine.validate_rule(engine.rules[rule_id].model_copy(update={"enabled": enabled}))

    def _change_rule(self, rule: Rule) -> list[Notification]:
        """Apply an already validated rule (disable/enable are updates with `enabled` flipped)."""
        rule_id = rule.rule_id
        before = self._mark()
        emitted = self._apply(lambda engine: engine.update_rule(rule))
        engine = self._live()
        self._persist(before, _Entry("rule_change", payload=rule.model_dump_json(), rule_id=rule_id,
                             template=_TEMPLATE_OF[type(rule)], rule_version=engine.rule_version(rule_id)))  # fmt: skip
        return emitted

    def _mark(self) -> tuple[dict[str, Incident], int]:
        """What the outputs looked like before an operation: incidents and notification count."""
        engine = self._live()
        return dict(engine.incidents), len(engine.notifications)

    def _apply(self, operation: Callable[[Engine], Any]) -> Any:
        """Run one mutating engine operation; pre-checks must already have passed.

        Any error from here may have left the engine half-changed, whatever its
        type, so the engine is rebuilt from the database before re-raising.
        """
        engine = self._live()
        try:
            return operation(engine)
        except Exception:
            self._rebuild()
            raise

    def _persist(self, before: tuple[dict[str, Incident], int], entry: _Entry) -> None:
        """Write the journal row and every output produced since `before`, in one transaction."""
        engine = self._live()
        old_incidents, old_count = before
        positions = {incident_id: i for i, incident_id in enumerate(engine.incidents)}
        changed = [i for i in engine.incidents.values() if old_incidents.get(i.incident_id) != i]
        try:
            with self._conn:
                self._conn.execute("BEGIN")
                self._conn.execute(
                    "INSERT INTO journal (kind, line_number, event_id, payload, outcome, rule_id, template, "
                    "rule_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (entry.kind, entry.line_number, entry.event_id, entry.payload, entry.outcome,
                     entry.rule_id, entry.template, entry.rule_version),
                )  # fmt: skip
                self._conn.executemany(
                    f"INSERT INTO incidents (position, {_INCIDENT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(incident_id) DO UPDATE SET recipient = excluded.recipient, "
                    "status = excluded.status, severity = excluded.severity, closed_at = excluded.closed_at",
                    [(positions[i.incident_id], *_incident_row(i)) for i in changed],
                )
                self._conn.executemany(
                    f"INSERT INTO notifications ({_NOTIFICATION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [_notification_row(n) for n in engine.notifications[old_count:]],
                )
        except sqlite3.Error as err:  # `with` rolled the transaction back
            self._rebuild()
            raise StorageError(f"write failed and was rolled back; state rebuilt from the journal: {err}") from err

    def _rebuild(self) -> None:
        """Replace the in-memory engine with one recovered from the database (Decision 50)."""
        try:
            self._engine = _recover(self._conn)
        except Exception as err:
            self._engine = None
            self._unusable = "rebuilding from the journal failed; reopen the store with Store.open"
            raise StorageError(self._unusable) from err


# ---------------------------------------------------------------------------
# Recovery and row conversion (module-level so open() and _rebuild() share them)
# ---------------------------------------------------------------------------


def _recover(conn: Any) -> Engine:
    """Replay the baseline and journal, then check the result against stored outputs."""
    baseline = conn.execute("SELECT rule_id, template, config, version FROM baseline_rules ORDER BY rule_id")
    journal = conn.execute(
        "SELECT seq, kind, line_number, payload, outcome, rule_id, template, rule_version FROM journal ORDER BY seq"
    ).fetchall()
    try:
        rules: list[Rule] = []
        versions: dict[str, int] = {}
        for rule_id, template, config, version in baseline.fetchall():
            rules.append(_TEMPLATES[template].model_validate_json(config))
            versions[rule_id] = version
        engine = Engine(rules, versions=versions)
        for seq, kind, line_number, payload, outcome, rule_id, template, rule_version in journal:
            if kind == "event":
                actual = engine.step(SourcedEvent(line_number, parse_event(payload))).outcome
                if actual != outcome:
                    raise RecoveryMismatchError(f"journal row {seq}: stored outcome {outcome}, replay gave {actual}")
            elif kind == "rule_change":
                engine.update_rule(_TEMPLATES[template].model_validate_json(payload))
                if engine.rule_version(rule_id) != rule_version:
                    raise RecoveryMismatchError(f"journal row {seq}: stored version {rule_version}, "
                                                f"replay gave {engine.rule_version(rule_id)}")  # fmt: skip
            elif kind == "finish":
                engine.finish()
            else:
                raise RecoveryMismatchError(f"journal row {seq}: unknown kind {kind!r}")
    except (ValueError, RuntimeError, KeyError) as err:
        if isinstance(err, RecoveryMismatchError):
            raise
        raise RecoveryMismatchError(f"the journal could not be replayed: {err}") from err

    if _load_notifications(conn) != engine.notifications:
        raise RecoveryMismatchError("stored notifications differ from the replayed ones")
    if _load_incidents(conn) != list(engine.incidents.values()):
        raise RecoveryMismatchError("stored incidents differ from the replayed ones")
    return engine


def _write_baseline(conn: Any, engine: Engine) -> None:
    conn.executemany(
        "INSERT INTO baseline_rules (rule_id, template, config, version) VALUES (?, ?, ?, ?)",
        [(rule_id, _TEMPLATE_OF[type(rule)], rule.model_dump_json(), engine.rule_version(rule_id))
         for rule_id, rule in engine.rules.items()],
    )  # fmt: skip


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _incident_row(i: Incident) -> tuple[Any, ...]:
    return (i.incident_id, i.rule_id, i.subject_id, i.recipient, i.status, i.severity,
            _iso(i.opened_at), _iso(i.closed_at))  # fmt: skip


def _notification_row(n: Notification) -> tuple[Any, ...]:
    return (n.seq, n.incident_id, n.rule_id, n.rule_version, n.subject_id, n.recipient, n.kind,
            n.severity, _iso(n.effective_at), _iso(n.emitted_at), n.line_number)  # fmt: skip


def _load_incidents(conn: Any) -> list[Incident]:
    rows = conn.execute(f"SELECT {_INCIDENT_COLUMNS} FROM incidents ORDER BY position").fetchall()
    return [
        Incident(incident_id, rule_id, subject_id, recipient, status, severity, _time(opened), _time(closed))
        for incident_id, rule_id, subject_id, recipient, status, severity, opened, closed in rows
    ]


def _load_notifications(conn: Any) -> list[Notification]:
    rows = conn.execute(f"SELECT {_NOTIFICATION_COLUMNS} FROM notifications ORDER BY seq").fetchall()
    return [
        Notification(seq, incident_id, rule_id, version, subject_id, recipient, kind, severity,
                     _time(effective), _time(emitted), line)  # fmt: skip
        for seq, incident_id, rule_id, version, subject_id, recipient, kind, severity, effective, emitted, line
        in rows
    ]
