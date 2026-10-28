import socket
from collections.abc import Callable

import pytest

from lab.models.types import CallRecord
from lab.scheduler.spec import RunSpec
from lab.store import RunRecord


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a network connection (DECISIONS.md D10)."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Network access is disabled in tests; use the fake model.")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


# --- a milestone-1 run record, shared by the store tests -----------------------------------
# Fixtures rather than module-level helpers, so no test file has to import from another.

MakeCall = Callable[..., CallRecord]
MakeRecord = Callable[[list[CallRecord]], RunRecord]


@pytest.fixture
def run_spec() -> RunSpec:
    """A benign milestone-1 spec: no attack fields, so its run ID is computed as in M1 (D48)."""
    return RunSpec(
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


@pytest.fixture
def make_call() -> MakeCall:
    """A CallRecord with usage numbers set; keyword arguments override any field."""

    def make(**overrides: object) -> CallRecord:
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

    return make


@pytest.fixture
def make_record(run_spec: RunSpec) -> MakeRecord:
    """A solved run of `run_spec` with the given calls, as milestone 1 stored it."""

    def make(calls: list[CallRecord]) -> RunRecord:
        return RunRecord(
            run_id=run_spec.run_id,
            spec=run_spec,
            config_hash="cfg",
            git_commit="deadbeef",
            agentdojo_version="0.1.35",
            seed=run_spec.seed,
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

    return make
