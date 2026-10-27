"""Metrics: Wilson CI against published values, percentiles, and grouping of stored runs."""

import pytest

from lab.cli import report_text
from lab.models.types import CallRecord
from lab.scheduler.spec import RunSpec
from lab.scoring.metrics import compute_flips, compute_metrics, percentile, wilson_interval
from lab.store import RunRecord


# Reference values: Newcombe (1998), "Two-sided confidence intervals for the single proportion",
# Statistics in Medicine 17:857-872, Table I (Wilson score method without continuity correction).
@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [
        (81, 263, 0.2553, 0.3662),
        (15, 148, 0.0624, 0.1605),
        (0, 20, 0.0, 0.1611),
        (1, 29, 0.0061, 0.1718),
    ],
)
def test_wilson_matches_newcombe_table(k: int, n: int, low: float, high: float) -> None:
    got_low, got_high = wilson_interval(k, n)
    assert got_low == pytest.approx(low, abs=1e-4)
    assert got_high == pytest.approx(high, abs=1e-4)


def test_wilson_edges() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)
    low, high = wilson_interval(16, 16)
    assert high == 1.0 and 0.80 < low < 0.81
    a, b = wilson_interval(5, 10)
    assert a == pytest.approx(1 - b)  # symmetric around 0.5


def test_percentile_interpolates_between_ranks() -> None:
    assert percentile([], 50) is None
    assert percentile([3.0], 95) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([float(i) for i in range(1, 21)], 95) == pytest.approx(19.05)


def spec(model_id: str = "m", task: str = "user_task_0") -> RunSpec:
    return RunSpec(
        experiment="e", suite="banking", benchmark_version="v1.2.2", user_task_id=task,
        topology="t1", provider="p", model_id=model_id, temperature=0.6,
        reasoning_effort="medium", max_tokens=2048, seed=1000, repeat_idx=0,
    )


def call(latency: float, *, cached: bool = False, tokens: int | None = 10) -> CallRecord:
    return CallRecord(
        provider="p", model_id="m", request_key="k", cached=cached, attempts=0 if cached else 1,
        input_tokens=tokens, output_tokens=5, reasoning_tokens=2, latency_s=latency,
        cost_usd=0.001, finish_reason="stop", malformed_tool_calls=0,
    )


def run(
    outcome: str,
    calls: list[CallRecord],
    *,
    subtype: str | None = None,
    malformed: int = 0,
    model_id: str = "m",
    task: str = "user_task_0",
) -> RunRecord:
    s = spec(model_id, task)
    return RunRecord(
        run_id=s.run_id + outcome, spec=s, config_hash="c", git_commit="g",
        agentdojo_version="0.1.35", seed=s.seed, outcome=outcome, outcome_subtype=subtype,
        utility=outcome == "solved", state_check_passed=None, error=None, calls=calls,
        malformed_tool_calls=malformed, messages=[], started_at="2026-10-25T00:00:00+00:00",
        finished_at="2026-10-25T00:00:01+00:00", wall_latency_s=1.0,
    )


def test_model_errors_count_as_not_solved_and_are_broken_down() -> None:
    runs = [
        run("solved", [call(1.0)]),
        run("not_solved", [call(1.0)], malformed=2),
        run("max_iterations", [call(1.0)]),
        run("no_final_answer", [call(1.0)]),
        run("model_error", [call(1.0)], subtype="truncated"),
        run("model_error", [call(1.0)], subtype="http_400"),
    ]
    [g] = compute_metrics(runs)
    assert (g.benign_utility.successes, g.benign_utility.n) == (1, 6)  # errors stay in n
    assert g.model_errors == {
        "http_400": 1, "max_iterations": 1, "no_final_answer": 1, "truncated": 1,
    }
    assert g.model_error_total == 4
    assert (g.malformed_tool_calls, g.runs_with_malformed_tool_calls) == (2, 1)


def test_latency_excludes_cache_hits() -> None:
    runs = [
        run("solved", [call(1.0), call(3.0)], task="user_task_0"),  # task latency 4.0
        run("solved", [call(0.5), call(9.0, cached=True)], task="user_task_1"),  # has a hit
    ]
    [g] = compute_metrics(runs)
    assert g.call_latency.n == 3  # the cached call is left out
    assert g.call_latency.p50 == 1.0
    assert g.task_latency.n == 1 and g.task_latency.p50 == 4.0  # only the uncached run
    assert (g.calls, g.cached_calls) == (4, 1)


def test_totals_and_groups() -> None:
    runs = [
        run("solved", [call(1.0), call(1.0)], model_id="a"),
        run("solved", [call(1.0, tokens=None)], model_id="b"),
    ]
    a, b = compute_metrics(runs)
    assert (a.model_id, a.input_tokens, a.output_tokens, a.reasoning_tokens) == ("a", 20, 10, 4)
    assert a.cost_usd == pytest.approx(0.002)
    assert b.input_tokens is None  # one call lacks usage: never guessed
    assert a.seeds == [1000]


def test_missing_reasoning_count_gives_a_range_not_a_guess() -> None:
    no_count = call(1.0).model_copy(update={"reasoning_tokens": None, "output_tokens": 73})
    [g] = compute_metrics([run("solved", [call(1.0), call(1.0), no_count])])
    assert g.reasoning_tokens is None  # the total is still never guessed (D25)
    assert g.calls_without_reasoning_tokens == 1
    assert g.reasoning_tokens_range == (4, 4 + 73)  # 0..73 for the call without a count (D40)
    [full] = compute_metrics([run("solved", [call(1.0)])])
    assert (full.calls_without_reasoning_tokens, full.reasoning_tokens_range) == (0, (2, 2))
    unknown = no_count.model_copy(update={"output_tokens": None})
    [none] = compute_metrics([run("solved", [unknown])])
    assert none.reasoning_tokens_range is None

    text = report_text("e", [g, full, none], stored=3, expected=3)
    assert "(reasoning 4 to 77; 1 of 3 calls had no count (D40))" in text
    assert "(reasoning 2)" in text
    assert "(reasoning unknown)" in text


def test_discriminating_utility_and_do_nothing_floor() -> None:
    runs = [
        run("solved", [call(1.0)], task="user_task_0"),
        run("not_solved", [call(1.0)], task="user_task_1"),
        run("solved", [call(1.0)], task="user_task_8"),  # a do-nothing agent passes it
        run("not_solved", [call(1.0)], task="user_task_9"),  # failed even though the floor passes
    ]
    [g] = compute_metrics(runs, do_nothing_passes=frozenset({"user_task_8", "user_task_9"}))
    assert (g.benign_utility.successes, g.benign_utility.n) == (2, 4)
    assert (g.discriminating_utility.successes, g.discriminating_utility.n) == (1, 2)
    assert (g.do_nothing_floor.successes, g.do_nothing_floor.n) == (2, 4)


def repeat(record: RunRecord, idx: int) -> RunRecord:
    s = record.spec.model_copy(update={"repeat_idx": idx, "seed": 1000 + idx})
    return record.model_copy(update={"spec": s, "run_id": s.run_id + record.outcome})


def test_flips_across_repeats_pool_experiments_and_rate_each_task() -> None:
    runs = [
        # user_task_0 solved on all three repeats: no flip.
        *(repeat(run("solved", [call(1.0)], task="user_task_0"), i) for i in range(3)),
        # user_task_3: solved, not, solved -> flips; 2 of 3 repeat pairs differ.
        repeat(run("solved", [call(1.0)], task="user_task_3"), 0),
        repeat(run("not_solved", [call(1.0)], task="user_task_3"), 1),
        repeat(run("solved", [call(1.0)], task="user_task_3"), 2),
        # user_task_10 only has repeat 0 so far: not counted in the flip rate.
        run("not_solved", [call(1.0)], task="user_task_10"),
    ]
    [f] = compute_flips(runs)
    assert [t.user_task_id for t in f.tasks] == ["user_task_0", "user_task_3", "user_task_10"]
    t0, t3, t10 = f.tasks
    assert (t0.flipped, t0.flip_rate) == (False, 0.0)
    assert t3.utilities == [(0, True), (1, False), (2, True)]
    assert t3.flipped and t3.flip_rate == pytest.approx(2 / 3)
    assert t10.flip_rate is None
    assert (f.tasks_flipped.successes, f.tasks_flipped.n) == (1, 2)

    text = report_text("a, b", compute_metrics(runs), stored=7, expected=None, flips=[f])
    assert "tasks that flip  1/2 = 50.0%" in text
    assert "user_task_3     r0:1 r1:0 r2:1     2/3     0.67  flips" in text


def test_no_flip_section_without_repeats() -> None:
    runs = [run("solved", [call(1.0)])]
    text = report_text("e", compute_metrics(runs), 1, 1, flips=compute_flips(runs))
    assert "tasks that flip" not in text


def test_report_shows_failure_labels_and_known_issues() -> None:
    labelled = run("not_solved", [call(1.0)], task="user_task_4").model_copy(
        update={"failure_label": "asked_user"}
    )
    unlabelled = run("not_solved", [call(1.0)], task="user_task_9")
    runs = [labelled, unlabelled, run("solved", [call(1.0)])]
    [g] = compute_metrics(runs)
    assert g.failure_labels == {"asked_user": 1, "unlabeled": 1}  # solved runs have no label
    text = report_text("e", [g], 3, 3, tasks={"user_task_4", "user_task_9"})
    assert "failure labels   asked_user 1, unlabeled 1" in text
    assert "Known issue, user_task_9: rewards inaction" in text
    assert "Known issue" not in report_text("e", [g], 3, 3, tasks={"user_task_4"})


def test_full_usage_has_no_estimate_lines_and_an_old_call_has_no_estimate() -> None:
    [full] = compute_metrics([run("solved", [call(1.0)])])
    assert full.calls_without_usage == 0 and full.tokens_estimate_high is None
    assert "no usage" not in report_text("e", [full], stored=1, expected=1)

    # A call without usage stored before D79 has no estimate: the range is unavailable, and
    # the report says so rather than guessing.
    [old] = compute_metrics([run("solved", [call(1.0), call(1.0, tokens=None)])])
    assert old.calls_without_usage == 1
    assert (old.known_tokens, old.tokens_estimate_high) == (15, None)
    assert "unavailable: a call has no stored estimate" in report_text(
        "e", [old], stored=1, expected=1
    )
