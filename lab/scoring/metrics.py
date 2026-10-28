"""Benign-utility metrics from stored runs (BRIEF §7, DECISIONS.md D34).

Computed per (provider, model, topology) group, from RunRecords only, so a report always
regenerates from the run store. Nothing is dropped:
- Every stored run is in the denominator of utility, model errors included, which count as not
  solved (D5, D21).
- Runs that never got an outcome (InfraError, daily cap) are not in the store. The report shows
  them as pending; they are never scored.

Utility is reported three ways (D39): on all runs (AgentDojo's own number), on runs of the
discriminating tasks only (the headline), and the do-nothing floor on the same runs.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from lab.domains.agentdojo_bridge.banking import DO_NOTHING_PASSES
from lab.domains.agentdojo_bridge.run_case import Outcome
from lab.models.providers import Price
from lab.models.types import CallRecord
from lab.store import RunRecord

Z_95 = 1.959963984540054  # standard normal quantile for a two-sided 95% interval

# Outcomes that are model errors under D5 / D21. The breakdown keys are these names, except that
# outcome "model_error" is split by its subtype ("truncated", "http_400", ...).
MODEL_ERROR_OUTCOMES = (Outcome.MAX_ITERATIONS, Outcome.NO_FINAL_ANSWER, Outcome.MODEL_ERROR)


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal ("Wald") interval because it stays inside [0, 1] and behaves well
    at 0/n and n/n, which small Banking samples hit. For n = 0 the interval is all of [0, 1].
    """
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denominator = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denominator
    half_width = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denominator
    return max(0.0, centre - half_width), min(1.0, centre + half_width)


def percentile(values: Sequence[float], q: float) -> float | None:
    """The q-th percentile (0-100), linear interpolation between ranks; None if empty.

    The same method as numpy's default ("linear"), written out to avoid a dependency.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * q / 100
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


@dataclass(frozen=True)
class Proportion:
    successes: int
    n: int

    @property
    def rate(self) -> float | None:
        return self.successes / self.n if self.n else None

    @property
    def ci95(self) -> tuple[float, float]:
        return wilson_interval(self.successes, self.n)


@dataclass(frozen=True)
class Latency:
    """p50 / p95 in seconds, over `n` samples. Cache hits are excluded (D34)."""

    n: int
    p50: float | None
    p95: float | None

    @classmethod
    def of(cls, values: Sequence[float]) -> Latency:
        return cls(n=len(values), p50=percentile(values, 50), p95=percentile(values, 95))


@dataclass(frozen=True)
class GroupMetrics:
    provider: str
    model_id: str
    topology: str
    benign_utility: Proportion  # all runs
    discriminating_utility: Proportion  # runs of tasks a do-nothing agent fails: the headline
    do_nothing_floor: Proportion  # what a do-nothing agent scores on these same runs
    outcomes: dict[str, int]  # every outcome, by name
    model_errors: dict[str, int]  # max_iterations, no_final_answer, and model_error subtypes
    malformed_tool_calls: int  # total over all runs
    runs_with_malformed_tool_calls: int
    # Calls to tools the agent doesn't have (D76), over the runs that counted them: None if no
    # run of the group did (all stored before D76).
    out_of_scope_tool_calls: int | None
    runs_with_out_of_scope_tool_calls: int
    # Groq-rejected calls recovered by kind (D82), summed from the stored call records.
    invalid_argument_tool_calls: int
    runs_with_invalid_argument_tool_calls: int
    unparsed_tool_calls: int
    runs_with_unparsed_tool_calls: int
    calls: int
    cached_calls: int
    requests_sent: int  # real provider requests, retries included
    # Totals are None if any run lacks the number: never guessed (D25).
    input_tokens: int | None
    output_tokens: int | None  # includes reasoning tokens (D19)
    reasoning_tokens: int | None
    # When some calls lack a reasoning count (D40): the reported counts summed, and that plus
    # those calls' output tokens, which bound what they spent. None if those also lack usage.
    calls_without_reasoning_tokens: int
    reasoning_tokens_range: tuple[int, int] | None
    cost_usd: float | None  # at public paid prices
    # Calls the provider sent no usage for (D79). Then the totals above stay None (D25), and
    # these give what is known and an estimated upper range beside them: known is the calls with
    # usage; the high end adds the limiter's pre-send estimate for the others. None if an
    # estimate is missing (calls stored before D79), or, for cost, the model has no price.
    calls_without_usage: int
    known_tokens: int  # input + output of the calls with usage
    known_cost_usd: float  # of the calls with a cost
    tokens_estimate_high: int | None
    cost_estimate_high_usd: float | None
    failure_labels: dict[str, int]  # hand labels of runs not solved; "unlabeled" for the rest
    call_latency: Latency  # per model call, cache hits excluded
    task_latency: Latency  # per run: sum of its call latencies, runs with no cache hit only
    seeds: list[int] = field(default_factory=list)
    git_commits: list[str] = field(default_factory=list)
    agentdojo_versions: list[str] = field(default_factory=list)
    benchmark_versions: list[str] = field(default_factory=list)
    first_run_at: str | None = None
    last_run_at: str | None = None

    @property
    def n(self) -> int:
        return self.benign_utility.n

    @property
    def model_error_total(self) -> int:
        return sum(self.model_errors.values())


def compute_metrics(
    records: Iterable[RunRecord],
    do_nothing_passes: frozenset[str] = DO_NOTHING_PASSES,
    prices: Mapping[tuple[str, str], Price] | None = None,
) -> list[GroupMetrics]:
    """`do_nothing_passes`: user task IDs whose utility check passes with no action taken.
    `prices`: (provider, model ID) -> price, for the estimated cost of calls without usage."""
    groups: dict[tuple[str, str, str], list[RunRecord]] = {}
    for record in records:
        key = (record.spec.provider, record.spec.model_id, record.spec.topology)
        groups.setdefault(key, []).append(record)
    return [
        _group(key, runs, do_nothing_passes, (prices or {}).get((key[0], key[1])))
        for key, runs in sorted(groups.items())
    ]


def _group(
    key: tuple[str, str, str],
    runs: list[RunRecord],
    do_nothing_passes: frozenset[str],
    price: Price | None,
) -> GroupMetrics:
    provider, model_id, topology = key
    model_errors: Counter[str] = Counter()
    for run in runs:
        if run.outcome == Outcome.MODEL_ERROR:
            model_errors[run.outcome_subtype or "unknown"] += 1
        elif run.outcome in MODEL_ERROR_OUTCOMES:
            model_errors[run.outcome] += 1

    failure_labels = Counter(run.failure_label or "unlabeled" for run in runs if not run.utility)
    discriminating = [run for run in runs if run.spec.user_task_id not in do_nothing_passes]
    calls = [c for run in runs for c in run.calls]
    unmeasured = [c for c in calls if _lacks_usage(c)]
    measured = [c for c in calls if not _lacks_usage(c)]
    uncached_runs = [run for run in runs if run.calls and not any(c.cached for c in run.calls)]
    started = sorted(run.started_at for run in runs)
    out_of_scope = [
        run.out_of_scope_tool_calls for run in runs if run.out_of_scope_tool_calls is not None
    ]
    return GroupMetrics(
        provider=provider,
        model_id=model_id,
        topology=topology,
        benign_utility=Proportion(successes=sum(run.utility for run in runs), n=len(runs)),
        discriminating_utility=Proportion(
            successes=sum(run.utility for run in discriminating), n=len(discriminating)
        ),
        do_nothing_floor=Proportion(successes=len(runs) - len(discriminating), n=len(runs)),
        outcomes=dict(sorted(Counter(run.outcome for run in runs).items())),
        model_errors=dict(sorted(model_errors.items())),
        malformed_tool_calls=sum(run.malformed_tool_calls for run in runs),
        runs_with_malformed_tool_calls=sum(run.malformed_tool_calls > 0 for run in runs),
        out_of_scope_tool_calls=sum(out_of_scope) if out_of_scope else None,
        runs_with_out_of_scope_tool_calls=sum(bool(run.out_of_scope_tool_calls) for run in runs),
        invalid_argument_tool_calls=sum(c.invalid_argument_tool_calls for c in calls),
        runs_with_invalid_argument_tool_calls=sum(
            any(c.invalid_argument_tool_calls for c in run.calls) for run in runs
        ),
        unparsed_tool_calls=sum(c.unparsed_tool_calls for c in calls),
        runs_with_unparsed_tool_calls=sum(
            any(c.unparsed_tool_calls for c in run.calls) for run in runs
        ),
        calls=len(calls),
        cached_calls=sum(c.cached for c in calls),
        requests_sent=sum(run.requests_sent for run in runs),
        input_tokens=_total(run.input_tokens for run in runs),
        output_tokens=_total(run.output_tokens for run in runs),
        reasoning_tokens=_total(run.reasoning_tokens for run in runs),
        calls_without_reasoning_tokens=sum(c.reasoning_tokens is None for c in calls),
        reasoning_tokens_range=_reasoning_range(calls),
        cost_usd=_total_float(run.cost_usd for run in runs),
        calls_without_usage=len(unmeasured),
        known_tokens=sum(_measured_tokens(c) for c in measured),
        known_cost_usd=sum(c.cost_usd for c in calls if c.cost_usd is not None),
        tokens_estimate_high=_tokens_high(unmeasured, measured),
        cost_estimate_high_usd=_cost_high(unmeasured, measured, price),
        failure_labels=dict(sorted(failure_labels.items())),
        call_latency=Latency.of([c.latency_s for c in calls if not c.cached]),
        task_latency=Latency.of([sum(c.latency_s for c in run.calls) for run in uncached_runs]),
        seeds=sorted({run.seed for run in runs if run.seed is not None}),
        git_commits=sorted({run.git_commit for run in runs}),
        agentdojo_versions=sorted({run.agentdojo_version for run in runs}),
        benchmark_versions=sorted({run.spec.benchmark_version for run in runs}),
        first_run_at=started[0] if started else None,
        last_run_at=started[-1] if started else None,
    )


@dataclass(frozen=True)
class TaskRepeats:
    """One task's outcomes over its repeats, for one model (D45)."""

    user_task_id: str
    utilities: list[tuple[int, bool]]  # (repeat_idx, solved), by repeat index

    @property
    def solved(self) -> int:
        return sum(u for _, u in self.utilities)

    @property
    def flipped(self) -> bool:
        return 0 < self.solved < len(self.utilities)

    @property
    def flip_rate(self) -> float | None:
        """Share of pairs of repeats whose outcomes differ; None with fewer than 2 repeats.

        0 means every repeat agreed. With 3 repeats, one odd one out gives 2/3.
        """
        n = len(self.utilities)
        if n < 2:
            return None
        return self.solved * (n - self.solved) / (n * (n - 1) / 2)


@dataclass(frozen=True)
class FlipMetrics:
    provider: str
    model_id: str
    topology: str
    tasks: list[TaskRepeats]  # in task order

    @property
    def repeated(self) -> list[TaskRepeats]:
        return [t for t in self.tasks if len(t.utilities) >= 2]

    @property
    def tasks_flipped(self) -> Proportion:
        """Tasks whose outcome was not the same on every repeat, of tasks run at least twice."""
        return Proportion(successes=sum(t.flipped for t in self.repeated), n=len(self.repeated))


def compute_flips(records: Iterable[RunRecord]) -> list[FlipMetrics]:
    """Per (provider, model, topology), each task's outcome on every repeat stored.

    Records may come from several experiments (e.g. a baseline and its added repeats): runs are
    matched by task and model, and each keeps its own repeat index.
    """
    groups: dict[tuple[str, str, str], dict[str, list[tuple[int, bool]]]] = {}
    for r in records:
        key = (r.spec.provider, r.spec.model_id, r.spec.topology)
        groups.setdefault(key, {}).setdefault(r.spec.user_task_id, []).append(
            (r.spec.repeat_idx, r.utility)
        )
    return [
        FlipMetrics(
            provider=provider,
            model_id=model_id,
            topology=topology,
            tasks=[
                TaskRepeats(task, sorted(utilities))
                for task, utilities in sorted(tasks.items(), key=lambda kv: _task_number(kv[0]))
            ],
        )
        for (provider, model_id, topology), tasks in sorted(groups.items())
    ]


def _task_number(task_id: str) -> int:
    return int(task_id.rsplit("_", 1)[1])


def _lacks_usage(call: CallRecord) -> bool:
    return call.input_tokens is None or call.output_tokens is None


def _measured_tokens(call: CallRecord) -> int:
    return (call.input_tokens or 0) + (call.output_tokens or 0)


def _estimated(unmeasured: Sequence[CallRecord]) -> list[tuple[int, int]] | None:
    """(input, output) estimates of the calls without usage; None if there are none to add, or
    one has no estimate (stored before D79)."""
    pairs = [(c.estimated_input_tokens, c.estimated_output_tokens) for c in unmeasured]
    if not pairs or any(i is None or o is None for i, o in pairs):
        return None
    return [(i, o) for i, o in pairs if i is not None and o is not None]


def _tokens_high(unmeasured: Sequence[CallRecord], measured: Sequence[CallRecord]) -> int | None:
    """The high end of the token range (D79): the known tokens plus the limiter's pre-send
    estimate for each call without usage."""
    pairs = _estimated(unmeasured)
    if pairs is None:
        return None
    return sum(_measured_tokens(c) for c in measured) + sum(i + o for i, o in pairs)


def _cost_high(
    unmeasured: Sequence[CallRecord], measured: Sequence[CallRecord], price: Price | None
) -> float | None:
    """The same for cost, at the model's public price; None without a price."""
    pairs = _estimated(unmeasured)
    if pairs is None or price is None:
        return None
    known = sum(c.cost_usd for c in measured if c.cost_usd is not None)
    return known + sum(price.cost_usd(i, o) for i, o in pairs)


def _reasoning_range(calls: Sequence[CallRecord]) -> tuple[int, int] | None:
    """Lowest and highest reasoning total the calls allow. A call without a reasoning count
    spent between 0 and its output tokens on reasoning (D19: output includes reasoning)."""
    known = sum(c.reasoning_tokens for c in calls if c.reasoning_tokens is not None)
    missing = [c.output_tokens for c in calls if c.reasoning_tokens is None]
    if None in missing:
        return None
    return known, known + sum(m for m in missing if m is not None)


def _total(values: Iterable[int | None]) -> int | None:
    items = list(values)
    return None if None in items else sum(v for v in items if v is not None)


def _total_float(values: Iterable[float | None]) -> float | None:
    items = list(values)
    return None if None in items else sum(v for v in items if v is not None)
