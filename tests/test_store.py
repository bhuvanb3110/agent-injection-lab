"""RunStore: round trip, insert-only, request history, totals that are never guessed."""

import sqlite3
from pathlib import Path

import pytest

from lab.models.types import CallRecord
from lab.scheduler.retry import RequestLogEntry
from lab.scheduler.spec import RunSpec
from lab.store import RunRecord, RunStore

SPEC = RunSpec(
    experiment="test",
    suite="banking",
    benchmark_version="v1.2.2",
    user_task_id="user_task_0",
    topology="t1",
    provider="groq",
    model_id="openai/gpt-oss-20b",
    temperature=0.6,
    reasoning_effort="medium",
    max_tokens=None,
    seed=1002,
    repeat_idx=2,
)


def call(**overrides: object) -> CallRecord:
    fields: dict[str, object] = {
        "provider": "groq",
        "model_id": "openai/gpt-oss-20b",
        "request_key": "abc",
        "cached": False,
        "attempts": 2,
        "input_tokens": 100,
        "output_tokens": 30,
        "reasoning_tokens": 12,
        "latency_s": 0.5,
        "cost_usd": 0.001,
        "finish_reason": "stop",
        "malformed_tool_calls": 1,
    }
    return CallRecord(**(fields | overrides))  # type: ignore[arg-type]


def record(calls: list[CallRecord]) -> RunRecord:
    return RunRecord(
        run_id=SPEC.run_id,
        spec=SPEC,
        config_hash="cfg",
        git_commit="deadbeef",
        agentdojo_version="0.1.35",
        seed=SPEC.seed,
        outcome="solved",
        outcome_subtype=None,
        utility=True,
        state_check_passed=True,
        error=None,
        calls=calls,
        malformed_tool_calls=sum(c.malformed_tool_calls for c in calls),
        messages=[{"role": "user", "content": [{"type": "text", "content": "Pay my bill."}]}],
        started_at="2026-10-25T00:00:00+00:00",
        finished_at="2026-10-25T00:00:03+00:00",
        wall_latency_s=3.0,
    )


def test_round_trip_keeps_every_field(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    original = record([call(), call(cached=True, attempts=0)])
    store.save_run(original)

    assert store.has_run(SPEC.run_id)
    assert store.get_run(SPEC.run_id) == original
    assert store.run_ids("test") == [SPEC.run_id]
    assert original.requests_sent == 2
    assert (original.input_tokens, original.output_tokens, original.reasoning_tokens) == (
        200,
        60,
        24,
    )


def test_flat_columns_are_queryable(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    store = RunStore(path)
    store.save_run(record([call()]))
    store.close()
    row = sqlite3.connect(path).execute(
        "SELECT agentdojo_version, benchmark_version, temperature, reasoning_effort, repeat_idx,"
        " outcome, malformed_tool_calls, n_calls, requests_sent FROM runs"
    ).fetchone()
    assert row == ("0.1.35", "v1.2.2", 0.6, "medium", 2, "solved", 1, 1, 2)


def test_a_run_is_stored_once(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    store.save_run(record([call()]))
    with pytest.raises(sqlite3.IntegrityError):
        store.save_run(record([call()]))


def test_totals_are_none_if_any_call_lacks_usage() -> None:
    rec = record([call(), call(input_tokens=None, cost_usd=None)])
    assert rec.input_tokens is None
    assert rec.cost_usd is None
    assert rec.output_tokens == 60


def test_request_history_is_per_model_and_windowed(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    for ts, model in [(10.0, "a"), (20.0, "a"), (30.0, "b")]:
        store.log_request(RequestLogEntry(ts, "groq", model, "ok", int(ts)))
    assert store.request_history("groq", "a", since=15.0) == [(20.0, 20)]
    assert store.request_statuses("groq", "b") == ["ok"]


def test_run_id_depends_on_every_spec_field() -> None:
    variants = [
        SPEC.model_copy(update={field: value})
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
    assert SPEC.run_id not in ids and len(ids) == len(variants)
