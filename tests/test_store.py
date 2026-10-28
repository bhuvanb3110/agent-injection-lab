"""RunStore: round trip, insert-only, request history, totals that are never guessed.

`run_spec`, `make_call` and `make_record` are fixtures in conftest.py.
"""

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from lab.models.types import CallRecord
from lab.scheduler.retry import RequestLogEntry
from lab.scheduler.spec import RunSpec
from lab.store import RunRecord, RunStore

MakeCall = Callable[..., CallRecord]
MakeRecord = Callable[[list[CallRecord]], RunRecord]


def test_round_trip_keeps_every_field(
    tmp_path: Path, run_spec: RunSpec, make_call: MakeCall, make_record: MakeRecord
) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    original = make_record([make_call(), make_call(cached=True, attempts=0)])
    store.save_run(original)

    assert store.has_run(run_spec.run_id)
    assert store.get_run(run_spec.run_id) == original
    assert store.run_ids("test") == [run_spec.run_id]
    assert original.requests_sent == 2
    assert (original.input_tokens, original.output_tokens, original.reasoning_tokens) == (
        200,
        60,
        24,
    )


def test_flat_columns_are_queryable(
    tmp_path: Path, make_call: MakeCall, make_record: MakeRecord
) -> None:
    path = tmp_path / "runs.sqlite"
    store = RunStore(path)
    store.save_run(make_record([make_call()]))
    store.close()
    row = sqlite3.connect(path).execute(
        "SELECT agentdojo_version, benchmark_version, temperature, reasoning_effort, repeat_idx,"
        " outcome, malformed_tool_calls, n_calls, requests_sent FROM runs"
    ).fetchone()
    assert row == ("0.1.35", "v1.2.2", 0.6, "medium", 2, "solved", 1, 1, 2)


def test_a_run_is_stored_once(
    tmp_path: Path, make_call: MakeCall, make_record: MakeRecord
) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    store.save_run(make_record([make_call()]))
    with pytest.raises(sqlite3.IntegrityError):
        store.save_run(make_record([make_call()]))


def test_totals_are_none_if_any_call_lacks_usage(
    make_call: MakeCall, make_record: MakeRecord
) -> None:
    rec = make_record([make_call(), make_call(input_tokens=None, cost_usd=None)])
    assert rec.input_tokens is None
    assert rec.cost_usd is None
    assert rec.output_tokens == 60


def test_request_history_is_per_model_and_windowed(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    for ts, model in [(10.0, "a"), (20.0, "a"), (30.0, "b")]:
        store.log_request(RequestLogEntry(ts, "groq", model, "ok", int(ts)))
    assert store.request_history("groq", "a", since=15.0) == [(20.0, 20)]
    assert store.request_statuses("groq", "b") == ["ok"]


def test_failed_request_errors_round_trip(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    failed = RequestLogEntry(1.0, "groq", "a", "config_error", 50, "ConfigError", 401, "HTTP 401")
    store.log_request(failed)
    store.log_request(RequestLogEntry(2.0, "groq", "a", "ok", 60))
    assert store.requests("groq", "a") == [failed, RequestLogEntry(2.0, "groq", "a", "ok", 60)]


def test_an_older_store_gains_the_error_columns(tmp_path: Path) -> None:
    # The requests table as it was before D42; its old rows keep NULL errors.
    path = tmp_path / "runs.sqlite"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
        " provider TEXT NOT NULL, model_id TEXT NOT NULL, status TEXT NOT NULL,"
        " tokens INTEGER NOT NULL)"
    )
    db.execute("INSERT INTO requests (ts, provider, model_id, status, tokens)"
               " VALUES (1.0, 'groq', 'a', 'config_error', 2342)")
    db.commit()
    db.close()

    store = RunStore(path)
    store.log_request(RequestLogEntry(2.0, "groq", "a", "http_400", 9, "ModelError", 400, "bad"))
    assert store.requests("groq", "a") == [
        RequestLogEntry(1.0, "groq", "a", "config_error", 2342),
        RequestLogEntry(2.0, "groq", "a", "http_400", 9, "ModelError", 400, "bad"),
    ]
    store.close()
    RunStore(path).close()  # opening again doesn't try to add the columns twice


def test_failure_label_is_the_one_update(
    tmp_path: Path, run_spec: RunSpec, make_call: MakeCall, make_record: MakeRecord
) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    original = make_record([make_call()])
    store.save_run(original)
    store.set_failure_label(run_spec.run_id, "asked_user")
    assert store.get_run(run_spec.run_id) == original.model_copy(
        update={"failure_label": "asked_user"}
    )
    with pytest.raises(KeyError):
        store.set_failure_label("no-such-run", "asked_user")


def test_run_id_depends_on_every_spec_field(run_spec: RunSpec) -> None:
    variants = [
        run_spec.model_copy(update={field: value})
        for field, value in [
            ("experiment", "other"),
            ("user_task_id", "user_task_1"),
            ("model_id", "openai/gpt-oss-120b"),
            ("temperature", 0.5),
            ("reasoning_effort", "low"),
            ("max_tokens", 100),
            ("seed", 7),
            ("repeat_idx", 0),
        ]
    ]
    ids = {v.run_id for v in variants}
    assert run_spec.run_id not in ids and len(ids) == len(variants)
