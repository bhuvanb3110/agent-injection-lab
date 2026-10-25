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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from lab.domains.agentdojo_bridge.banking import DO_NOTHING_PASSES
from lab.domains.agentdojo_bridge.run_case import Outcome
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
    calls: int
    cached_calls: int
    requests_sent: int  # real provider requests, retries included
    # Totals are None if any run lacks the number: never guessed (D25).
    input_tokens: int | None
    output_tokens: int | None  # includes reasoning tokens (D19)
    reasoning_tokens: int | None
    cost_usd: float | None  # at public paid prices
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
    records: Iterable[RunRecord], do_nothing_passes: frozenset[str] = DO_NOTHING_PASSES
) -> list[GroupMetrics]:
    """`do_nothing_passes`: user task IDs whose utility check passes with no action taken."""
    groups: dict[tuple[str, str, str], list[RunRecord]] = {}
    for record in records:
        key = (record.spec.provider, record.spec.model_id, record.spec.topology)
        groups.setdefault(key, []).append(record)
    return [_group(key, runs, do_nothing_passes) for key, runs in sorted(groups.items())]


def _group(
    key: tuple[str, str, str], runs: list[RunRecord], do_nothing_passes: frozenset[str]
) -> GroupMetrics:
    provider, model_id, topology = key
    model_errors: Counter[str] = Counter()
    for run in runs:
        if run.outcome == Outcome.MODEL_ERROR:
            model_errors[run.outcome_subtype or "unknown"] += 1
        elif run.outcome in MODEL_ERROR_OUTCOMES:
            model_errors[run.outcome] += 1

    discriminating = [run for run in runs if run.spec.user_task_id not in do_nothing_passes]
    calls = [c for run in runs for c in run.calls]
    uncached_runs = [run for run in runs if run.calls and not any(c.cached for c in run.calls)]
    started = sorted(run.started_at for run in runs)
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
        calls=len(calls),
        cached_calls=sum(c.cached for c in calls),
        requests_sent=sum(run.requests_sent for run in runs),
        input_tokens=_total(run.input_tokens for run in runs),
        output_tokens=_total(run.output_tokens for run in runs),
        reasoning_tokens=_total(run.reasoning_tokens for run in runs),
        cost_usd=_total_float(run.cost_usd for run in runs),
        call_latency=Latency.of([c.latency_s for c in calls if not c.cached]),
        task_latency=Latency.of([sum(c.latency_s for c in run.calls) for run in uncached_runs]),
        seeds=sorted({run.seed for run in runs if run.seed is not None}),
        git_commits=sorted({run.git_commit for run in runs}),
        agentdojo_versions=sorted({run.agentdojo_version for run in runs}),
        benchmark_versions=sorted({run.spec.benchmark_version for run in runs}),
        first_run_at=started[0] if started else None,
        last_run_at=started[-1] if started else None,
    )


def _total(values: Iterable[int | None]) -> int | None:
    items = list(values)
    return None if None in items else sum(v for v in items if v is not None)


def _total_float(values: Iterable[float | None]) -> float | None:
    items = list(values)
    return None if None in items else sum(v for v in items if v is not None)
