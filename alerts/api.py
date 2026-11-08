"""HTTP API and single-page UI over the SQLite Store.

The API is thin: parse the request, call one Store method, shape the JSON.
It holds no alert logic and no replay state of its own; the cursor (the next
source event) is the number of `event` rows in the journal, so it always
agrees with the database, including after a restart or a failed write.

Single process, single worker. Every endpoint is `async def`, so it runs on
the event-loop thread that opened the SQLite connection; SQLite's
check_same_thread safety stays on. Do not add plain `def` endpoints.
No endpoint awaits between reading Store state and changing it, so each
request's Store work runs to completion without interleaving.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from alerts.ingest import SourcedEvent, load_events
from alerts.presentation import (
    TEMPLATE_NAMES,
    describe_validation_error,
    incident_json,
    iso,
    merge_diagnostics,
    notification_json,
    rule_json,
)
from alerts.routing import RoutingError, seeded_rules
from alerts.storage import JournalRow, StorageError, Store

log = logging.getLogger(__name__)

_PACKAGE = Path(__file__).resolve().parent
DEFAULT_EVENTS = _PACKAGE.parent / "data" / "events.jsonl"
PAGE = _PACKAGE / "static" / "index.html"

STORAGE_ERROR_DETAIL = "The alert store could not complete this operation. See the server log for details."
RESET_MESSAGE = "Replay history cleared; current rule configurations and versions are kept."
FINISHED_DETAIL = "The replay is finished. Press Reset to clear history and replay again."
_MISMATCH_HINT = "Restore the original events file, or delete the database file to start a new replay."


class SourceMismatchError(RuntimeError):
    """The events file no longer matches the replay history stored in the database."""


def verify_source(journal: list[JournalRow], events: list[SourcedEvent]) -> None:
    """Every journaled event (accepted, duplicate or stale) must match the source line by line.

    Lines appended after the journaled prefix are allowed. A missing payload
    fails verification rather than being skipped.
    """
    journaled = [row for row in journal if row.kind == "event"]
    if len(journaled) > len(events):
        raise SourceMismatchError(f"The replay history has {len(journaled)} events but the source has "
                                  f"{len(events)} readable events. {_MISMATCH_HINT}")  # fmt: skip
    for row, sourced in zip(journaled, events):
        if row.payload is None:
            raise SourceMismatchError(f"line {row.line_number}: the replay history has no stored event, "
                                      f"so the source cannot be verified. {_MISMATCH_HINT}")  # fmt: skip
        if row.line_number != sourced.line_number or row.payload != sourced.event.model_dump_json():
            raise SourceMismatchError(f"line {row.line_number} of the source no longer matches the replay "
                                      f"history (journaled event {row.event_id}). {_MISMATCH_HINT}")  # fmt: skip


def create_app(db_path: Path | None = None, events_path: Path | None = None) -> FastAPI:
    """Build the app. The Store is opened (or created) at startup and closed at shutdown."""
    db = Path(db_path) if db_path is not None else Path(os.environ.get("ALERTS_DB", "alerts.db"))
    source = Path(events_path) if events_path is not None else DEFAULT_EVENTS

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loaded = load_events(source)
        for rejected in loaded.rejected:
            log.warning("events line %d skipped: %s", rejected.line_number, rejected.reason)
        store = Store.open(db) if db.exists() else Store.create(db, seeded_rules())
        try:
            verify_source(store.journal(), loaded.accepted)
        except BaseException:
            store.close()
            raise
        app.state.store = store
        app.state.events = loaded.accepted
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title="Assembled intraday alerts", lifespan=lifespan)

    def store() -> Store:
        result: Store = app.state.store
        return result

    def events() -> list[SourcedEvent]:
        result: list[SourcedEvent] = app.state.events
        return result

    def cursor() -> int:
        return sum(1 for row in store().journal() if row.kind == "event")

    def status() -> dict[str, Any]:
        engine, journal, total = store().engine, store().journal(), len(events())
        outcomes = [row.outcome for row in journal if row.kind == "event"]
        position = len(outcomes)
        if engine.finished:
            next_kind = "done"
        elif position < total:
            next_kind = "event"
        else:
            next_kind = "end_of_input"
        timers = sorted(engine.pending_timers().items(), key=lambda item: (item[1], item[0]))
        return {
            "cursor": position,
            "total_events": total,
            "next": next_kind,
            "next_line": events()[position].line_number if next_kind == "event" else None,
            "clock": iso(engine.state.clock),
            "finished": engine.finished,
            "counts": {
                "accepted": outcomes.count("accepted"),
                "duplicate": outcomes.count("duplicate"),
                "stale": outcomes.count("stale"),
                "active_incidents": len(engine.active_incidents()),
                "notifications": len(engine.notifications),
                "pending_timers": len(timers),
            },
            "pending_timers": [{"rule_id": rule_id, "subject_id": subject_id, "due_at": iso(due)}
                               for (rule_id, subject_id), due in timers],  # fmt: skip
        }

    def reject_if_finished() -> None:
        if store().engine.finished:
            raise HTTPException(status_code=409, detail=FINISHED_DETAIL)

    @app.exception_handler(StorageError)
    async def storage_failed(request: Request, err: StorageError) -> JSONResponse:
        log.error("storage failure during %s %s", request.method, request.url.path, exc_info=err)
        return JSONResponse(status_code=500, content={"detail": STORAGE_ERROR_DETAIL})

    # -- page and reads -----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def page() -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

    @app.get("/replay/status")
    async def replay_status() -> dict[str, Any]:
        return status()

    @app.get("/rules")
    async def list_rules() -> list[dict[str, Any]]:
        engine = store().engine
        return [rule_json(engine.rules[rule_id], engine.rule_version(rule_id)) for rule_id in sorted(engine.rules)]

    @app.get("/incidents")
    async def list_incidents() -> list[dict[str, Any]]:
        return [incident_json(incident) for incident in store().stored_incidents()]

    @app.get("/notifications")
    async def list_notifications() -> list[dict[str, Any]]:
        return [notification_json(n) for n in store().stored_notifications()]

    @app.get("/diagnostics")
    async def list_diagnostics() -> list[dict[str, Any]]:
        engine = store().engine
        return merge_diagnostics(store().journal(), engine.state.diagnostics, engine.diagnostics)

    # -- replay controls ----------------------------------------------------

    @app.post("/replay/step")
    async def step() -> dict[str, Any]:
        """Process the next source event; at end of input, finish the replay instead."""
        reject_if_finished()
        position = cursor()
        if position < len(events()):
            sourced = events()[position]
            result = store().step(sourced)
            body: dict[str, Any] = {"kind": "event", "line_number": sourced.line_number,
                                    "outcome": result.outcome, "notifications": result.notifications}  # fmt: skip
        else:
            body = {"kind": "finish", "line_number": None, "outcome": None, "notifications": store().finish()}
        body["notifications"] = [notification_json(n) for n in body["notifications"]]
        body["status"] = status()
        return body

    @app.post("/replay/run-all")
    async def run_all() -> dict[str, Any]:
        """Process every remaining event, then finish. Returns only this call's notifications."""
        reject_if_finished()
        before = len(store().stored_notifications())
        store().run_all(events()[cursor():])
        created = store().stored_notifications()[before:]
        return {"notifications": [notification_json(n) for n in created], "status": status()}

    @app.post("/replay/reset")
    async def reset() -> dict[str, Any]:
        store().reset()
        return {"message": RESET_MESSAGE, "status": status()}

    # -- rule editing -------------------------------------------------------

    @app.patch("/rules/{rule_id}")
    async def edit_rule(rule_id: str, request: Request) -> dict[str, Any]:
        """Merge the given fields into the current rule, validate it fully, and apply it via the Store."""
        # Read the body first: after this there is no `await`, so no other request can
        # change the rule between reading it and writing the edit.
        try:
            changes = await request.json()
        except ValueError:
            changes = None
        engine = store().engine
        if rule_id not in engine.rules:
            raise HTTPException(status_code=404, detail=f"Unknown rule {rule_id!r}.")
        if not isinstance(changes, dict):
            raise HTTPException(status_code=422, detail="The request body must be a JSON object of rule fields.")
        if not changes:
            raise HTTPException(status_code=422, detail="The request body must change at least one field.")

        current = engine.rules[rule_id]
        for field, value in (("rule_id", rule_id), ("template", TEMPLATE_NAMES[type(current)])):
            if field in changes and changes[field] != value:
                raise HTTPException(status_code=422, detail=f"{field} cannot be changed.")
        changes.pop("template", None)  # not a config field; it only names the rule's type
        try:
            store().update_rule(type(current).model_validate({**current.model_dump(), **changes}))
        except ValidationError as err:
            raise HTTPException(status_code=422, detail=describe_validation_error(err)) from err
        except RoutingError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        engine = store().engine
        return rule_json(engine.rules[rule_id], engine.rule_version(rule_id))

    return app


app = create_app()
