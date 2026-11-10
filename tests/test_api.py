"""Phase 4 — HTTP API over the SQLite Store, exercised through FastAPI's TestClient.

Every test gets a fresh database in tmp_path. A "restart" closes the client
and builds a new app on the same database file, so Store.open recovers it.

Response contract (the shapes these tests rely on):
  status:        {cursor, total_events, next ("event" | "end_of_input" | "done"), next_line,
                  clock, finished, counts: {accepted, duplicate, stale, active_incidents,
                  notifications, pending_timers}, pending_timers: [{rule_id, subject_id, due_at}]}
  step:          {kind ("event" | "finish"), line_number, outcome, notifications, status}
  run-all:       {notifications, status}        reset: {message, status}
  rule:          {rule_id, template, version, enabled, preview, config}
  notification:  {seq, incident_id, rule_id, rule_version, subject_id, recipient, kind,
                  severity, effective_at, emitted_at, line_number}
  incident:      {incident_id, rule_id, subject_id, recipient, status, severity, opened_at, closed_at}
  diagnostic:    {line_number, kind, subject_id, detail, source}
  error:         {detail}
Times are ISO 8601 strings.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alerts.api import SourceMismatchError, create_app
from alerts.storage import StorageError, Store
from tests.builders import snapshot, state_change
from tests.engine_builders import hm
from tests.test_dataset_oracle import ORACLE
from tests.test_rule_creation import R4_NEW_ROWS, R4_TOTAL

DATA = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"
DATA_LINES = DATA.read_text(encoding="utf-8").splitlines()
TOTAL = len(DATA_LINES)  # 96

BACKLOG_BILLING = "backlog.lead_billing"
LONG_CALL_BILLING = "long_call.lead_billing"
STORAGE_ERROR_DETAIL = "The alert store could not complete this operation. See the server log for details."
RESET_MESSAGE = "Replay history cleared; current rule configurations and versions are kept."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def running(db: Path, events: Path = DATA) -> Iterator[TestClient]:
    """A client whose app has started (so the Store is open) and is shut down afterwards."""
    with TestClient(create_app(db, events)) as client:
        yield client


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "alerts.db"


@pytest.fixture
def client(db: Path) -> Iterator[TestClient]:
    with running(db) as client:
        yield client


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def status(client: TestClient) -> dict[str, Any]:
    return ok(client.get("/replay/status"))


def step(client: TestClient, times: int = 1) -> dict[str, Any]:
    """Step `times` times; return the last step's response."""
    result: dict[str, Any] = {}
    for _ in range(times):
        result = ok(client.post("/replay/step"))
    return result


def patch(client: TestClient, rule_id: str, changes: dict[str, Any]) -> Any:
    return client.patch(f"/rules/{rule_id}", json=changes)


def rule(client: TestClient, rule_id: str) -> dict[str, Any]:
    return next(r for r in ok(client.get("/rules")) if r["rule_id"] == rule_id)


def clock(value: str | None) -> str | None:
    return hm(datetime.fromisoformat(value)) if value is not None else None


def oracle_rows(notifications: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """The same tuple shape as ORACLE: (emitted, effective, rule, subject, kind, severity, recipient)."""
    return [
        (clock(n["emitted_at"]), clock(n["effective_at"]), n["rule_id"], n["subject_id"], n["kind"],
         n["severity"], n["recipient"])
        for n in notifications
    ]  # fmt: skip


def everything(client: TestClient) -> dict[str, Any]:
    """Every read endpoint, for before/after comparisons."""
    return {path: ok(client.get(path)) for path in
            ("/replay/status", "/rules", "/incidents", "/notifications", "/diagnostics")}  # fmt: skip


def write_events(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def changed(line: str, **fields: Any) -> str:
    record = json.loads(line)
    record.update(fields)
    return json.dumps(record)


def shifted_ts(line: str, minutes: int = 1) -> str:
    ts = datetime.fromisoformat(json.loads(line)["ts"]) + timedelta(minutes=minutes)
    return changed(line, ts=ts.isoformat())


# ---------------------------------------------------------------------------
# A. Startup and status
# ---------------------------------------------------------------------------


def test_fresh_status(client: TestClient) -> None:
    assert status(client) == {
        "cursor": 0,
        "total_events": TOTAL,
        "next": "event",
        "next_line": 1,
        "clock": None,
        "finished": False,
        "counts": {"accepted": 0, "duplicate": 0, "stale": 0, "active_incidents": 0,
                   "notifications": 0, "pending_timers": 0},  # fmt: skip
        "pending_timers": [],
    }


def test_seeded_rules_listed(client: TestClient) -> None:
    rules = ok(client.get("/rules"))
    assert len(rules) == 8
    assert {(r["version"], r["enabled"]) for r in rules} == {(1, True)}
    assert {r["template"] for r in rules} == {"backlog", "sla", "adherence", "long_call"}
    assert all(r["preview"].startswith("Notify ") for r in rules)
    assert rule(client, BACKLOG_BILLING)["config"]["warning_above"] == 15


def test_page_served_with_controls_and_explanations(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    for marker in ('id="step"', 'id="run-all"', 'id="reset"', "Effective", "Emitted",
                   "Edits apply from the current simulated clock onward", RESET_MESSAGE):  # fmt: skip
        assert marker in page, marker


# ---------------------------------------------------------------------------
# B. Stepping and end of input
# ---------------------------------------------------------------------------


def test_first_step(client: TestClient) -> None:
    result = step(client)
    assert (result["kind"], result["line_number"], result["outcome"], result["notifications"]) == (
        "event", 1, "accepted", [],
    )  # fmt: skip
    assert clock(result["status"]["clock"]) == "09:00"
    assert (result["status"]["cursor"], result["status"]["next_line"]) == (1, 2)


def test_step_returns_only_its_own_notifications(client: TestClient) -> None:
    result = step(client, 76)
    assert result["line_number"] == 76
    assert [n["seq"] for n in result["notifications"]] == [9, 10, 11]
    a07 = result["notifications"][0]
    assert (a07["subject_id"], clock(a07["effective_at"]), clock(a07["emitted_at"])) == ("a_07", "10:10", "10:10:30")


def test_duplicate_and_stale_steps_are_visible_and_inert(client: TestClient) -> None:
    step(client, 94)
    duplicate = step(client)
    stale = step(client)
    assert [(r["line_number"], r["outcome"], r["notifications"]) for r in (duplicate, stale)] == [
        (95, "duplicate", []),
        (96, "stale", []),
    ]
    assert clock(stale["status"]["clock"]) == "10:30"


def test_after_last_line_next_step_is_end_of_input(client: TestClient) -> None:
    step(client, TOTAL)
    current = status(client)
    assert (current["cursor"], current["next"], current["next_line"], current["finished"]) == (
        TOTAL, "end_of_input", None, False,
    )  # fmt: skip
    assert current["counts"]["notifications"] == 19
    assert (current["counts"]["accepted"], current["counts"]["duplicate"], current["counts"]["stale"]) == (94, 1, 1)


def test_end_of_input_step_finishes(client: TestClient) -> None:
    step(client, TOTAL)
    result = step(client)
    assert (result["kind"], result["line_number"], result["outcome"]) == ("finish", None, None)
    assert [(n["seq"], n["rule_id"], n["subject_id"], n["kind"], n["line_number"]) for n in result["notifications"]] == [
        (20, "long_call.lead_support", "a_23", "opened", None),
    ]
    assert (result["status"]["finished"], result["status"]["next"]) == (True, "done")


def test_step_after_finish_is_rejected_and_changes_nothing(client: TestClient) -> None:
    step(client, TOTAL + 1)
    before = everything(client)
    response = client.post("/replay/step")
    assert response.status_code == 409
    assert "Reset" in response.json()["detail"]
    assert everything(client) == before


# ---------------------------------------------------------------------------
# C. Run All and Reset
# ---------------------------------------------------------------------------


def test_run_all_matches_oracle(client: TestClient) -> None:
    result = ok(client.post("/replay/run-all"))
    assert oracle_rows(result["notifications"]) == ORACLE
    assert oracle_rows(ok(client.get("/notifications"))) == ORACLE
    active = [(i["rule_id"], i["subject_id"]) for i in ok(client.get("/incidents")) if i["status"] == "active"]
    assert sorted(active) == [
        ("adherence.lead_support", "a_23"),
        ("adherence.lead_support", "a_88"),
        ("long_call.lead_billing", "a_07"),
        ("long_call.lead_support", "a_23"),
    ]
    final = result["status"]
    assert final["finished"] is True
    assert [(t["rule_id"], t["subject_id"], clock(t["due_at"])) for t in final["pending_timers"]] == [
        ("long_call.lead_support", "a_42", "10:33"),
    ]


def test_run_all_from_mid_replay_returns_only_the_rest(client: TestClient) -> None:
    step(client, 50)  # notifications 1-3 so far (the last at line 49)
    result = ok(client.post("/replay/run-all"))
    assert [n["seq"] for n in result["notifications"]] == list(range(4, 21))
    assert oracle_rows(ok(client.get("/notifications"))) == ORACLE


def test_run_all_when_finished_is_rejected(client: TestClient) -> None:
    ok(client.post("/replay/run-all"))
    before = everything(client)
    assert client.post("/replay/run-all").status_code == 409
    assert everything(client) == before


def test_reset_clears_history_but_keeps_rules(client: TestClient) -> None:
    ok(patch(client, BACKLOG_BILLING, {"warning_above": 12}))
    ok(client.post("/replay/run-all"))
    result = ok(client.post("/replay/reset"))
    assert result["message"] == RESET_MESSAGE
    assert (result["status"]["cursor"], result["status"]["finished"], result["status"]["clock"]) == (0, False, None)
    assert ok(client.get("/notifications")) == []
    assert ok(client.get("/incidents")) == []
    assert ok(client.get("/diagnostics")) == []
    kept = rule(client, BACKLOG_BILLING)
    assert (kept["version"], kept["config"]["warning_above"]) == (2, 12)


def test_reset_then_rerun_is_identical(client: TestClient) -> None:
    first = ok(client.post("/replay/run-all"))["notifications"]
    ok(client.post("/replay/reset"))
    second = ok(client.post("/replay/run-all"))["notifications"]
    assert second == first


# ---------------------------------------------------------------------------
# D. Rule editing
# ---------------------------------------------------------------------------


def test_valid_edit_bumps_version_and_preview(client: TestClient) -> None:
    edited = ok(patch(client, BACKLOG_BILLING, {"warning_above": 12}))
    assert (edited["version"], edited["config"]["warning_above"]) == (2, 12)
    assert "more than 12 tickets waiting" in edited["preview"]
    assert rule(client, BACKLOG_BILLING) == edited


def test_invalid_threshold_rejected(client: TestClient) -> None:
    response = patch(client, BACKLOG_BILLING, {"warning_above": 25})
    assert response.status_code == 422
    assert "warning_above must be less than critical_above" in response.json()["detail"]
    assert rule(client, BACKLOG_BILLING)["version"] == 1


def test_routing_requires_cross_owner_for_another_leads_queue(client: TestClient) -> None:
    scope = {"scope": {"queues": ["billing"]}}
    response = patch(client, "sla.lead_support", scope)
    assert response.status_code == 422
    assert "owned by lead_billing" in response.json()["detail"]
    assert "cross_owner" in response.json()["detail"]
    accepted = ok(patch(client, "sla.lead_support", {**scope, "cross_owner": True}))
    assert (accepted["version"], accepted["config"]["scope"]) == (2, {"queues": ["billing"]})


@pytest.mark.parametrize(
    ("changes", "named"),
    [({"rule_id": "renamed"}, "rule_id"), ({"template": "sla"}, "template"),
     ({"bogus": 1}, "bogus"), ({"warning_above": "12"}, "warning_above")],
    ids=["rule-id", "template", "unknown-field", "wrong-type"],
)  # fmt: skip
def test_unchangeable_or_malformed_fields_rejected(client: TestClient, changes: dict[str, Any], named: str) -> None:
    before = everything(client)
    response = patch(client, BACKLOG_BILLING, changes)
    assert response.status_code == 422
    assert named in response.json()["detail"]
    assert everything(client) == before


def test_empty_patch_rejected(client: TestClient) -> None:
    before = everything(client)
    response = patch(client, BACKLOG_BILLING, {})
    assert response.status_code == 422
    assert "at least one field" in response.json()["detail"]
    assert everything(client) == before  # no version bump, no journal row


def test_unknown_rule_is_404(client: TestClient) -> None:
    assert patch(client, "nope", {"enabled": False}).status_code == 404


def test_disable_closes_active_incident_silently(client: TestClient) -> None:
    step(client, 39)  # billing backlog opened at line 39 (notification 1)
    disabled = ok(patch(client, BACKLOG_BILLING, {"enabled": False}))
    assert (disabled["enabled"], disabled["version"]) == (False, 2)
    assert disabled["preview"].startswith("Disabled: ")
    incident = next(i for i in ok(client.get("/incidents")) if i["rule_id"] == BACKLOG_BILLING)
    assert (incident["incident_id"], incident["status"]) == (f"{BACKLOG_BILLING}:billing:1", "disabled")
    assert len(ok(client.get("/notifications"))) == 2  # no notification from the edit


# ---------------------------------------------------------------------------
# E. Persistence, restart and the replay cursor
# ---------------------------------------------------------------------------


def test_restart_mid_replay_with_interleaved_rule_edit(db: Path) -> None:
    with running(db) as client:
        step(client, 30)
        ok(patch(client, BACKLOG_BILLING, {"warning_above": 12}))
        step(client, 10)
        before = everything(client)
    with running(db) as client:
        assert everything(client) == before
        assert status(client)["cursor"] == 40  # the rule-change row is not an event
        assert rule(client, BACKLOG_BILLING)["version"] == 2
        assert step(client)["line_number"] == 41


def test_restart_after_duplicate_continues_at_the_stale_line(db: Path) -> None:
    with running(db) as client:
        step(client, 95)  # line 95 is the duplicate
    with running(db) as client:
        current = status(client)
        assert (current["cursor"], current["next_line"]) == (95, 96)
        assert (current["counts"]["accepted"], current["counts"]["duplicate"], current["counts"]["stale"]) == (94, 1, 0)
        result = step(client)
        assert (result["line_number"], result["outcome"]) == (96, "stale")
        assert result["status"]["counts"]["stale"] == 1


def test_restart_after_finish_stays_finished(db: Path) -> None:
    with running(db) as client:
        ok(client.post("/replay/run-all"))
    with running(db) as client:
        assert status(client)["finished"] is True
        assert client.post("/replay/step").status_code == 409
        assert oracle_rows(ok(client.get("/notifications"))) == ORACLE


def test_restart_after_reset_keeps_edited_rules_and_empty_history(db: Path) -> None:
    with running(db) as client:
        ok(patch(client, BACKLOG_BILLING, {"warning_above": 12}))
        ok(client.post("/replay/run-all"))
        ok(client.post("/replay/reset"))
    with running(db) as client:
        assert (status(client)["cursor"], ok(client.get("/notifications"))) == (0, [])
        kept = rule(client, BACKLOG_BILLING)
        assert (kept["version"], kept["config"]["warning_above"]) == (2, 12)


# ---------------------------------------------------------------------------
# F. Startup verification: the source must still match the journaled prefix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", [1, 39, 95, 96], ids=["first", "middle", "duplicate", "stale"])
def test_changed_timestamp_of_processed_event_fails_startup(tmp_path: Path, db: Path, line: int) -> None:
    events = write_events(tmp_path / "events.jsonl", DATA_LINES)
    with running(db, events) as client:
        step(client, TOTAL)
    lines = list(DATA_LINES)
    lines[line - 1] = shifted_ts(lines[line - 1])  # same event_id, different ts
    write_events(events, lines)
    with pytest.raises(SourceMismatchError, match=rf"line {line}\b.*no longer matches|no longer matches.*line {line}\b"):
        with running(db, events):
            pass


@pytest.mark.parametrize(
    ("line", "field", "value"),
    [(39, "tickets_waiting", 19),  # billing snapshot, 18 -> 19
     (95, "actual_state", "available")],  # the duplicate adherence check, on_break -> available
    ids=["accepted", "duplicate"],
)  # fmt: skip
def test_changed_payload_of_processed_event_fails_startup(
    tmp_path: Path, db: Path, line: int, field: str, value: Any
) -> None:
    events = write_events(tmp_path / "events.jsonl", DATA_LINES)
    with running(db, events) as client:
        step(client, TOTAL)
    lines = list(DATA_LINES)
    assert json.loads(lines[line - 1])[field] != value  # the edit really changes the event
    lines[line - 1] = changed(lines[line - 1], **{field: value})
    write_events(events, lines)
    with pytest.raises(SourceMismatchError, match=rf"line {line}\b"):
        with running(db, events):
            pass


def test_source_shorter_than_history_fails_startup(tmp_path: Path, db: Path) -> None:
    events = write_events(tmp_path / "events.jsonl", DATA_LINES[:10])
    with running(db, events) as client:
        step(client, 10)
    write_events(events, DATA_LINES[:5])
    with pytest.raises(SourceMismatchError, match="replay history has 10 events but the source has 5"):
        with running(db, events):
            pass


def test_appended_lines_continue_an_unfinished_replay(tmp_path: Path, db: Path) -> None:
    events = write_events(tmp_path / "events.jsonl", DATA_LINES[:5])
    with running(db, events) as client:
        step(client, 5)
    write_events(events, DATA_LINES[:7])
    with running(db, events) as client:
        current = status(client)
        assert (current["cursor"], current["total_events"], current["next_line"]) == (5, 7, 6)
        result = step(client)
        assert (result["line_number"], result["outcome"]) == (6, "accepted")


def test_appended_lines_never_reopen_a_finished_replay(tmp_path: Path, db: Path) -> None:
    events = write_events(tmp_path / "events.jsonl", DATA_LINES[:5])
    with running(db, events) as client:
        ok(client.post("/replay/run-all"))
    write_events(events, DATA_LINES[:7])
    with running(db, events) as client:
        current = status(client)
        assert (current["finished"], current["cursor"], current["total_events"], current["next"]) == (
            True, 5, 7, "done",
        )  # fmt: skip
        before = everything(client)
        assert client.post("/replay/step").status_code == 409
        assert client.post("/replay/run-all").status_code == 409
        assert everything(client) == before
        ok(client.post("/replay/reset"))  # only an explicit reset replays the longer source
        assert ok(client.post("/replay/run-all"))["status"]["cursor"] == 7


# ---------------------------------------------------------------------------
# G. Diagnostics and errors
# ---------------------------------------------------------------------------


def test_dataset_diagnostics_in_line_order_and_stable_across_restart(db: Path) -> None:
    with running(db) as client:
        ok(client.post("/replay/run-all"))
        diagnostics = ok(client.get("/diagnostics"))
        assert [(d["line_number"], d["kind"], d["subject_id"], d["source"]) for d in diagnostics] == [
            (68, "forecast_missing", "billing", "replay"),
            (86, "conflicting_activity", "a_23", "replay"),
            (90, "duration_mismatch", "a_31", "replay"),
            (95, "duplicate_event", "evt_01HXYZ050", "journal"),
            (95, "duplicate_payload_differs", "evt_01HXYZ050", "replay"),
            (96, "stale_event", "evt_01HXYZ096", "journal"),
        ]
    with running(db) as client:
        assert ok(client.get("/diagnostics")) == diagnostics  # rebuilt once, not doubled


def test_engine_diagnostics_from_synthetic_source(tmp_path: Path, db: Path) -> None:
    lines = [
        state_change("09:00", "a_99", "on_call").model_dump_json(),  # nobody owns a_99
        state_change("09:10", "a_11", "on_call").model_dump_json(),
        snapshot("10:05", "tier_2").model_dump_json(),  # fires a_11's 09:55 long-call timer
        state_change("09:50", "a_11", "available", previous_state="on_call").model_dump_json(),  # late exit
    ]
    events = write_events(tmp_path / "events.jsonl", lines)
    with running(db, events) as client:
        ok(client.post("/replay/run-all"))
        diagnostics = ok(client.get("/diagnostics"))
    assert [(d["line_number"], d["kind"], d["subject_id"], d["source"]) for d in diagnostics] == [
        (1, "unowned_subject", "a_99", "engine"),
        (4, "alert_contradicted_by_late_evidence", "a_11", "engine"),
    ]


def test_unknown_agent_state_diagnostic_visible_and_stable_across_restart(tmp_path: Path, db: Path) -> None:
    lines = [
        state_change("09:00", "a_11", "on_call").model_dump_json(),
        state_change("09:20", "a_11", "lunch", previous_state="on_call").model_dump_json(),  # unfamiliar new_state
        state_change("09:30", "a_11", "offline", previous_state="lunch").model_dump_json(),  # unfamiliar previous_state
    ]
    events = write_events(tmp_path / "events.jsonl", lines)
    with running(db, events) as client:
        status = ok(client.post("/replay/run-all"))["status"]
        assert (status["counts"]["accepted"], status["counts"]["notifications"]) == (3, 0)
        diagnostics = ok(client.get("/diagnostics"))
    assert [(d["line_number"], d["kind"], d["subject_id"], d["source"]) for d in diagnostics] == [
        (2, "unknown_agent_state", "a_11", "replay"),
        (3, "unknown_agent_state", "a_11", "replay"),
    ]
    with running(db, events) as client:
        assert ok(client.get("/diagnostics")) == diagnostics  # rebuilt from the journal, not doubled


def test_storage_failure_gives_safe_message(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_step(*_: Any) -> None:
        raise StorageError("disk I/O error writing /secret/path/alerts.db")

    monkeypatch.setattr(client.app.state.store, "step", failing_step)  # type: ignore[attr-defined]
    response = client.post("/replay/step")
    assert response.status_code == 500
    assert response.json() == {"detail": STORAGE_ERROR_DETAIL}
    assert "disk I/O" not in response.text and "/secret" not in response.text


def test_every_endpoint_runs_on_the_sqlite_thread(client: TestClient) -> None:
    # A plain `def` endpoint would run on a worker thread and SQLite would raise.
    calls = [
        ("GET", "/", None), ("GET", "/replay/status", None), ("GET", "/rules", None),
        ("GET", "/incidents", None), ("GET", "/notifications", None), ("GET", "/diagnostics", None),
        ("POST", "/replay/step", None), ("PATCH", f"/rules/{BACKLOG_BILLING}", {"enabled": True}),
        ("POST", "/replay/run-all", None), ("POST", "/replay/reset", None),
    ]  # fmt: skip
    for method, path, body in calls:
        response = client.request(method, path, json=body)
        assert response.status_code == 200, (method, path, response.text)


# ---------------------------------------------------------------------------
# H. Main user workflow
# ---------------------------------------------------------------------------


def test_demo_workflow_edit_run_reset_restore(client: TestClient) -> None:
    # 1. Step to line 68: a_11's long call fires, detected at 09:55, reported at 10:00.
    result = step(client, 68)
    a11 = next(n for n in result["notifications"] if n["subject_id"] == "a_11")
    assert (clock(a11["effective_at"]), clock(a11["emitted_at"])) == ("09:55", "10:00")

    # 2. Lower lead_billing's long-call threshold to 30 minutes at clock 10:00.
    #    a_07 (on call since 09:25) and a_31 (since 09:30) are now past 30 minutes,
    #    so both timers become max(due, 10:00) = 10:00. Strict firing skips lines
    #    69-70 (also 10:00) and fires both at line 71 (10:01), in subject order.
    ok(patch(client, LONG_CALL_BILLING, {"min_duration_sec": 1800}))
    ok(client.post("/replay/run-all"))
    run = oracle_rows(ok(client.get("/notifications")))  # the full history, not just Run All's new rows
    a07_early = ("10:01", "10:00", LONG_CALL_BILLING, "a_07", "opened", None, "lead_billing")
    a31_early = ("10:01", "10:00", LONG_CALL_BILLING, "a_31", "opened", None, "lead_billing")
    # ORACLE[8] is a_07 opened at 10:10:30 and ORACLE[13] is a_31 opened at 10:15:30;
    # both move earlier. Everything else is unchanged.
    assert run == [*ORACLE[:8], a07_early, a31_early, *ORACLE[9:13], *ORACLE[14:]]
    by_seq = {n["seq"]: n for n in ok(client.get("/notifications"))}
    assert (by_seq[9]["line_number"], by_seq[9]["rule_version"]) == (71, 2)

    # 3. Reset keeps the edit; restoring 45 minutes reproduces the oracle exactly.
    ok(client.post("/replay/reset"))
    assert rule(client, LONG_CALL_BILLING)["config"]["min_duration_sec"] == 1800
    restored = ok(patch(client, LONG_CALL_BILLING, {"min_duration_sec": 2700}))
    assert restored["version"] == 3
    assert oracle_rows(ok(client.post("/replay/run-all"))["notifications"]) == ORACLE


# ---------------------------------------------------------------------------
# I. Phase 5.3 — creating rules (POST /rules)
# ---------------------------------------------------------------------------

R1_BODY = {"template": "backlog", "recipient": "lead_billing", "scope": {"queues": ["billing"]},
           "warning_above": 20, "critical_above": 25}  # fmt: skip
R4_BODY = {"template": "long_call", "recipient": "lead_support", "scope": "all_owned", "min_duration_sec": 1800}
OVERLAP_NOTE = "A new rule works independently. If two rules cover the same queue or agent, the lead may get one alert from each."
ENABLED_NOTE = ("New rules are enabled by default, even when copied from a disabled rule. "
                "You can disable the new rule after creating it.")


def create(client: TestClient, body: Any) -> Any:
    return client.post("/rules", json=body)


def created(response: Any) -> Any:
    assert response.status_code == 201, response.text
    return response.json()


def new_rows(notifications: list[dict[str, Any]], rule_id: str) -> list[tuple[Any, ...]]:
    """The same shape as the R*_NEW_ROWS constants in tests/test_rule_creation.py."""
    return [
        (n["seq"], n["line_number"], n["rule_id"], n["subject_id"], n["kind"], n["severity"],
         clock(n["effective_at"]), clock(n["emitted_at"]), n["rule_version"])
        for n in notifications if n["rule_id"] == rule_id
    ]  # fmt: skip


def test_create_rule_returns_201_with_generated_id(client: TestClient) -> None:
    body = created(create(client, R1_BODY))
    assert body == {
        "rule_id": "backlog.lead_billing.2",
        "template": "backlog",
        "version": 1,
        "enabled": True,
        "preview": "Notify lead_billing when any listed queue (billing) has more than 20 tickets waiting; "
                   "critical above 25.",
        "config": {"rule_id": "backlog.lead_billing.2", "recipient": "lead_billing", "cross_owner": False,
                   "enabled": True, "scope": {"queues": ["billing"]}, "warning_above": 20, "critical_above": 25},
    }  # fmt: skip
    assert len(ok(client.get("/rules"))) == 9
    assert rule(client, "backlog.lead_billing.2") == body


def test_create_rule_uses_template_defaults(client: TestClient) -> None:
    body = created(create(client, {"template": "long_call", "recipient": "lead_support", "scope": "all_owned"}))
    assert body["rule_id"] == "long_call.lead_support.2"
    assert body["config"]["min_duration_sec"] == 2700  # PLAN §4 default


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({**R1_BODY, "rule_id": "backlog.lead_billing.2"}, "rule_id is assigned by the server."),
        ({**R1_BODY, "template": "forecast"}, "template"),
        ({k: v for k, v in R1_BODY.items() if k != "template"}, "template"),
        ({k: v for k, v in R1_BODY.items() if k != "recipient"}, "recipient"),
        ({**R1_BODY, "colour": "red"}, "colour"),
        ({**R1_BODY, "warning_above": "20"}, "warning_above"),
        ({**R1_BODY, "warning_above": 25, "critical_above": 20}, "critical_above"),
        ({**R1_BODY, "scope": {"queues": ["tier_2"]}}, "tier_2"),  # owned by lead_support
        ([R1_BODY], None),
    ],
    ids=["rule-id-given", "unknown-template", "missing-template", "missing-recipient", "unknown-field",
         "wrong-type", "critical-not-above-warning", "cross-owner-without-flag", "not-an-object"],
)  # fmt: skip
def test_create_rule_rejections(db: Path, body: Any, detail: str | None) -> None:
    with running(db) as client:
        before = everything(client)
        response = create(client, body)
        assert response.status_code == 422, response.text
        if detail is not None:
            assert detail in str(response.json()["detail"])
        assert everything(client) == before
    store = Store.open(db)  # the app's connection is closed; read the journal directly
    assert store.journal() == []
    store.close()


def test_created_rule_can_be_patched(client: TestClient) -> None:
    rule_id = created(create(client, {**R4_BODY, "min_duration_sec": 2700}))["rule_id"]
    edited_rule = ok(patch(client, rule_id, {"min_duration_sec": 1800}))
    assert (edited_rule["version"], edited_rule["config"]["min_duration_sec"]) == (2, 1800)


def test_create_after_finish_then_reset_runs_it(client: TestClient) -> None:
    ok(client.post("/replay/run-all"))
    finished = status(client)
    rule_id = created(create(client, R4_BODY))["rule_id"]
    assert rule_id == "long_call.lead_support.2"
    assert status(client) == finished  # nothing scheduled or emitted after finish

    ok(client.post("/replay/reset"))
    ok(client.post("/replay/run-all"))
    notifications = ok(client.get("/notifications"))
    assert new_rows(notifications, rule_id) == R4_NEW_ROWS
    assert [row for row in oracle_rows(notifications) if row[2] != rule_id] == ORACLE
    assert len(notifications) == R4_TOTAL


def test_created_rule_survives_server_restart(db: Path) -> None:
    with running(db) as client:
        body = created(create(client, R1_BODY))
    with running(db) as client:
        assert rule(client, body["rule_id"]) == body


def test_post_rules_runs_on_the_sqlite_thread(client: TestClient) -> None:
    # Separate from test_every_endpoint_runs_on_the_sqlite_thread, which expects 200 everywhere.
    assert create(client, R1_BODY).status_code == 201


def test_page_offers_new_rule_and_overlap_note(client: TestClient) -> None:
    page = client.get("/").text
    assert "New rule from this one" in page
    assert ENABLED_NOTE in page
    assert OVERLAP_NOTE in page  # numeric card order (.2 ... .10) is checked in a real browser, not here
