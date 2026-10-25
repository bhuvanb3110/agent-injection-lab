"""Scheduler end to end: real Banking task, FakeModel, fake clock, SQLite store.

Each run is user_task_0 (pay a bill) through T1: LabLLM -> ScheduledClient -> FakeModel.
"""

import random
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from lab.domains.agentdojo_bridge import BENCHMARK_VERSION, load_banking_suite, run_case
from lab.domains.agentdojo_bridge.run_case import CaseResult
from lab.errors import InfraError, ModelError
from lab.models.fake import FakeModel, Step, answer, call
from lab.models.llm_element import LabLLM
from lab.models.providers import Limits
from lab.models.types import ModelResponse, RunContext
from lab.scheduler.clock import FakeClock
from lab.scheduler.limits import DAY_S, MINUTE_S, RateLimiter
from lab.scheduler.queue import Provenance, Scheduler, SchedulerReport
from lab.scheduler.retry import RetryPolicy, ScheduledClient
from lab.scheduler.spec import RunSpec
from lab.store import RunStore
from lab.topologies.t1 import build_t1

PROVIDER, MODEL = "groq", "openai/gpt-oss-20b"
READ_BILL = {"file_path": "bill-december-2023.txt"}
PAY = {"recipient": "UK12345678901234567890", "amount": 98.70, "subject": "Car Rental",
       "date": "2022-01-01"}
PROVENANCE = Provenance(config_hash="cfg", git_commit="deadbeef", agentdojo_version="0.1.35")


def spec(repeat_idx: int = 0, max_tokens: int | None = None) -> RunSpec:
    return RunSpec(
        experiment="test",
        suite="banking",
        benchmark_version=BENCHMARK_VERSION,
        user_task_id="user_task_0",
        topology="t1",
        provider=PROVIDER,
        model_id=MODEL,
        temperature=0.6,
        reasoning_effort="medium",
        max_tokens=max_tokens,
        seed=1000 + repeat_idx,
        repeat_idx=repeat_idx,
    )


def limits(**caps: int) -> Limits:
    return Limits(
        rpm=caps.get("rpm"),
        rpd=caps.get("rpd"),
        tpm=caps.get("tpm"),
        tpd=caps.get("tpd"),
        source="test",
        checked=date(2026, 10, 25),
        verified_for_free_plan=True,
    )


def tokens(response: ModelResponse, n: int) -> ModelResponse:
    """The same response, reporting n tokens in total (all but 100 of them input)."""
    return response.model_copy(update={"input_tokens": n - 100, "output_tokens": 100})


def solve(tokens_per_call: int | None = None) -> list[Step]:
    """The three responses that solve user_task_0, optionally reporting more tokens each."""
    steps = [call("read_file", READ_BILL), call("send_money", PAY), answer("I paid the bill.")]
    if tokens_per_call is None:
        return list(steps)
    return [tokens(r, tokens_per_call) for r in steps]


def http(code: int, retry_after_s: float | None = None) -> InfraError:
    return InfraError(f"HTTP {code}", provider=PROVIDER, status_code=code,
                      retry_after_s=retry_after_s)


class Lab:
    """One invocation of `lab run`: a fresh limiter seeded from the store, a scheduler."""

    def __init__(self, path: Path, clock: FakeClock, lim: Limits, *, max_requeues: int = 1):
        self.store = RunStore(path)
        self.clock = clock
        self.limiter = RateLimiter(
            PROVIDER, MODEL, lim, clock,
            history=self.store.request_history(PROVIDER, MODEL, since=clock.now() - DAY_S),
        )
        self.suite = load_banking_suite()
        self.max_requeues = max_requeues

    def run(
        self, specs: list[RunSpec], script: Callable[[RunSpec], list[Step]]
    ) -> SchedulerReport:
        def run_fn(s: RunSpec, ctx: RunContext) -> CaseResult:
            client = ScheduledClient(
                FakeModel(script(s)), self.limiter, self.clock,
                retry=RetryPolicy(rng=random.Random(0)), log=self.store.log_request,
            )
            llm = LabLLM(client, provider=s.provider, model_id=s.model_id,
                         temperature=s.temperature, max_tokens=s.max_tokens,
                         reasoning_effort=s.reasoning_effort, seed=s.seed)
            pipeline = build_t1(llm, system_message="You are a helpful banking assistant.")
            return run_case(self.suite, pipeline, self.suite.get_user_task_by_id(s.user_task_id),
                            ctx)

        return Scheduler(self.store, run_fn, self.clock, PROVENANCE,
                         max_requeues=self.max_requeues).run(specs)


def never_called(_: RunSpec) -> list[Step]:
    raise AssertionError("this run should have been skipped")


def assert_windows_respected(store: RunStore, lim: Limits) -> None:
    """Replay the request log: no 60 s or 24 h window ever held more than the limits."""
    log = store.request_history(PROVIDER, MODEL, since=0.0)
    for ts, _ in log:
        for window, max_requests, max_tokens in ((MINUTE_S, lim.rpm, lim.tpm),
                                                  (DAY_S, lim.rpd, lim.tpd)):
            inside = [(t, n) for t, n in log if ts - window < t <= ts]
            if max_requests is not None:
                assert len(inside) <= max_requests
            if max_tokens is not None:
                assert sum(n for _, n in inside) <= max_tokens


def test_request_and_token_limits_are_respected(tmp_path: Path) -> None:
    clock = FakeClock()
    # Each call really uses 2000 tokens; the limiter estimates ~3K before sending.
    lim = limits(rpm=2, tpm=6000)
    lab = Lab(tmp_path / "runs.sqlite", clock, lim)
    report = lab.run([spec(0), spec(1)], lambda _: solve(tokens_per_call=2000))

    assert len(report.completed) == 2
    assert all(lab.store.get_run(r).outcome == "solved" for r in report.completed)
    assert clock.sleeps  # it had to wait
    assert all(s <= MINUTE_S for s in clock.sleeps)  # minute limits never stop the run
    assert_windows_respected(lab.store, lim)


def test_retry_on_429_then_success(tmp_path: Path) -> None:
    lab = Lab(tmp_path / "runs.sqlite", FakeClock(), limits())
    report = lab.run([spec()], lambda _: [http(429, retry_after_s=2.0), *solve()])

    run = lab.store.get_run(report.completed[0])
    assert run.outcome == "solved"
    assert len(run.calls) == 3
    assert run.calls[0].attempts == 2
    assert run.requests_sent == 4
    assert lab.store.request_statuses(PROVIDER, MODEL) == ["http_429", "ok", "ok", "ok"]


def test_infra_error_after_retries_requeues_and_never_scores(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    clock = FakeClock()
    lab = Lab(path, clock, limits(), max_requeues=1)
    target = spec()
    report = lab.run([target], lambda _: [http(503)] * 5)

    assert report.completed == []
    assert report.requeues == 1  # went back on the queue once...
    assert report.deferred == [target.run_id]  # ...then left for a later invocation
    assert not lab.store.has_run(target.run_id)  # never scored
    assert lab.store.request_statuses(PROVIDER, MODEL) == ["http_503"] * 10  # 2 tries x 5

    later = Lab(path, clock, limits()).run([target], lambda _: solve())
    assert later.completed == [target.run_id]


def test_daily_token_cap_stops_cleanly_and_a_later_invocation_resumes(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    clock = FakeClock()
    lim = limits(tpd=10_000)  # each run uses 3 x 2000; one run plus a bit fits per day
    specs = [spec(i, max_tokens=100) for i in range(3)]
    ids = [s.run_id for s in specs]

    def script(_: RunSpec) -> list[Step]:
        return solve(tokens_per_call=2000)

    first = Lab(path, clock, lim).run(specs, script)
    assert first.completed == [ids[0]]
    assert first.not_started == [ids[1], ids[2]]  # run 1 was cut mid-run, run 2 never began
    assert f"{PROVIDER}/{MODEL}" in first.stopped_models
    assert not Lab(path, clock, lim).store.has_run(ids[1])  # the cut run was not scored

    # Same moment, new invocation: the day's usage is read back from the store.
    again = Lab(path, clock, lim).run(specs, script)
    assert again.completed == [] and again.skipped == [ids[0]]

    clock.advance(DAY_S)
    second = Lab(path, clock, lim).run(specs, script)
    assert second.skipped == [ids[0]]
    assert second.completed == [ids[1]]  # restarted from scratch and finished

    clock.advance(DAY_S)
    third = Lab(path, clock, lim).run(specs, script)
    assert third.completed == [ids[2]] and third.pending == []
    assert_windows_respected(RunStore(path), lim)


def test_resume_skips_completed_runs(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    clock = FakeClock()
    specs = [spec(0), spec(1), spec(0)]  # a duplicate spec is the same run
    first = Lab(path, clock, limits()).run(specs, lambda _: solve())
    assert len(first.completed) == 2

    second = Lab(path, clock, limits()).run(specs, never_called)
    assert second.completed == []
    assert sorted(second.skipped) == sorted(first.completed)


def test_model_errors_are_recorded_never_dropped(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    clock = FakeClock()
    error = ModelError("HTTP 400 from groq: bad request", provider=PROVIDER,
                       subtype="http_400", status_code=400)
    target = spec()
    report = Lab(path, clock, limits()).run([target], lambda _: [call("read_file", READ_BILL),
                                                                  error])
    assert report.completed == [target.run_id]

    run = RunStore(path).get_run(target.run_id)
    assert run.outcome == "model_error"
    assert run.outcome_subtype == "http_400"
    assert run.utility is False  # counts as not solved in headline utility (D21)
    assert run.state_check_passed is None
    assert run.error is not None and "400" in run.error
    assert len(run.calls) == 1  # the call before the error is kept
    # So is the transcript up to the failing call: prompt, read_file call and its result (D30).
    assert [m["role"] for m in run.messages] == ["system", "user", "assistant", "tool"]
    assert run.messages[2]["tool_calls"][0]["function"] == "read_file"

    # A model error is final: it is not retried on resume.
    again = Lab(path, clock, limits()).run([target], never_called)
    assert again.skipped == [target.run_id]


def test_truncated_response_is_a_model_error_with_its_partial_message(tmp_path: Path) -> None:
    cut = answer("I paid the bi").model_copy(update={"finish_reason": "length"})
    target = spec(max_tokens=256)
    lab = Lab(tmp_path / "runs.sqlite", FakeClock(), limits())
    lab.run([target], lambda _: [call("read_file", READ_BILL), cut])

    run = lab.store.get_run(target.run_id)
    assert (run.outcome, run.outcome_subtype, run.utility) == ("model_error", "truncated", False)
    assert len(run.calls) == 2  # the truncated call is counted
    assert run.calls[-1].finish_reason == "length"
    assert run.messages[-1]["content"] == [{"type": "text", "content": "I paid the bi"}]


def test_run_record_has_provenance_sampling_and_trace(tmp_path: Path) -> None:
    lab = Lab(tmp_path / "runs.sqlite", FakeClock(), limits())
    report = lab.run([spec(repeat_idx=2)], lambda _: solve())
    run = lab.store.get_run(report.completed[0])

    assert (run.git_commit, run.agentdojo_version, run.config_hash) == (
        "deadbeef", "0.1.35", "cfg")
    assert run.spec.benchmark_version == "v1.2.2"
    assert run.seed == run.spec.seed == 1002  # base seed 1000 + repeat 2 (D27)
    assert (run.spec.temperature, run.spec.reasoning_effort, run.spec.repeat_idx) == (
        0.6, "medium", 2)
    assert run.input_tokens == 30 and run.output_tokens == 15  # 3 calls x (10 in, 5 out)
    assert run.malformed_tool_calls == 0
    assert [m["role"] for m in run.messages][-1] == "assistant"
    assert run.messages[2]["tool_calls"][0]["function"] == "read_file"  # FunctionCall as JSON


@pytest.mark.parametrize("cap", ["rpd", "tpd"])
def test_other_models_keep_going_when_one_is_capped(tmp_path: Path, cap: str) -> None:
    # Limits are per model: a capped model must not stop runs that use a different model.
    lab = Lab(tmp_path / "runs.sqlite", FakeClock(), limits(**{cap: 1}))
    other_limiter = RateLimiter(PROVIDER, "openai/gpt-oss-120b", limits(), lab.clock)
    capped, free = spec(), spec().model_copy(update={"model_id": "openai/gpt-oss-120b"})

    def run_fn(s: RunSpec, ctx: RunContext) -> CaseResult:
        limiter = lab.limiter if s.model_id == MODEL else other_limiter
        client = ScheduledClient(FakeModel(solve()), limiter, lab.clock)
        llm = LabLLM(client, provider=s.provider, model_id=s.model_id)
        pipeline = build_t1(llm, system_message="x")
        return run_case(lab.suite, pipeline, lab.suite.get_user_task_by_id(s.user_task_id), ctx)

    report = Scheduler(lab.store, run_fn, lab.clock, PROVENANCE).run([capped, free])
    assert report.not_started == [capped.run_id]
    assert report.completed == [free.run_id]
