"""`lab run <experiment.yaml>`, `lab report <experiment>...` and `lab label <labels.yaml>` (D35).

`lab run` is resumable: runs already in the store are skipped, so after a daily cap the same
command, run again once the 24 h window has room, carries on where it stopped.

Secrets: keys are read from `.env` (or the real environment) into a dict handed to the client.
They are never printed, and never put in os.environ, a cache key, a trace or a run record.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence, Set
from pathlib import Path

from dotenv import dotenv_values

from lab.config import ExperimentConfig, expand, load_experiment
from lab.domains.agentdojo_bridge import DO_NOTHING_PASSES, KNOWN_ISSUES
from lab.errors import ConfigError
from lab.models.cache import ResponseCache
from lab.models.providers import load_providers
from lab.runner import ExperimentRunner, TokenBudget, model_key
from lab.scheduler.clock import SystemClock
from lab.scheduler.spec import RunSpec
from lab.scoring.failure_labels import apply_labels, load_labels
from lab.scoring.metrics import (
    FlipMetrics,
    GroupMetrics,
    Latency,
    Proportion,
    compute_flips,
    compute_metrics,
)
from lab.store import RunStore

DEFAULT_STORE = Path("results/runs.sqlite")
DEFAULT_CACHE = Path("cache/responses.sqlite")
DEFAULT_EXPERIMENTS_DIR = Path("configs/experiments")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run an experiment; resumes where it stopped")
    run.add_argument("config", type=Path, help="configs/experiments/<name>.yaml")
    run.add_argument("--store", type=Path, default=DEFAULT_STORE)
    run.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    run.add_argument("--env-file", type=Path, default=Path(".env"))

    report = commands.add_parser(
        "report",
        help="metrics for one or more experiments from the run store; with several, their runs "
        "are pooled, and per-task flips across repeats are shown (D45)",
    )
    report.add_argument("experiments", nargs="+", help="experiment names (each config's `name`)")
    report.add_argument("--store", type=Path, default=DEFAULT_STORE)
    report.add_argument(
        "--config",
        type=Path,
        default=None,
        help="with one experiment: config to count pending runs against "
        "(default: configs/experiments/<name>.yaml)",
    )

    label = commands.add_parser("label", help="write hand failure labels into the run store")
    label.add_argument("labels", type=Path, help="configs/labels/<experiment>.yaml")
    label.add_argument("--store", type=Path, default=DEFAULT_STORE)

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args.config, args.store, args.cache, args.env_file)
        if args.command == "label":
            return cmd_label(args.labels, args.store)
        if args.config is not None and len(args.experiments) > 1:
            parser.error("--config takes a single experiment")
        return cmd_report(args.experiments, args.store, args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2


def cmd_run(config_path: Path, store_path: Path, cache_path: Path, env_file: Path) -> int:
    config = load_experiment(config_path)
    store = RunStore(store_path)
    cache = ResponseCache(cache_path)
    try:
        runner = ExperimentRunner(
            config, load_providers(), store, cache, SystemClock(), _environ(env_file)
        )
        pending = runner.pending()
        total = len(runner.specs)
        print(f"Experiment {config.name}: {total} runs, {total - len(pending)} already done, "
              f"{len(pending)} to go. Code version {_short(runner.provenance().git_commit)}.")
        _print_budgets(runner)

        done = total - len(pending)

        def progress(spec: RunSpec, event: str) -> None:
            nonlocal done
            stored = event not in ("requeued", "deferred", "daily cap")
            if stored:
                done += 1
            ref = next(m for m in config.models
                       if (m.provider, m.model_id) == (spec.provider, spec.model_id))
            left = _fmt_tokens(runner.token_budget(ref).remaining)
            print(f"[{done}/{total}] {model_key(spec.provider, spec.model_id)} "
                  f"{spec.user_task_id} r{spec.repeat_idx}: {event}  "
                  f"(tokens left today: {left})", flush=True)

        result = runner.run(progress)
        print(f"Done this invocation: {len(result.completed)} stored, "
              f"{len(result.pending)} pending, {result.requeues} requeued.")
        for model, reason in result.stopped_models.items():
            print(f"  {model} stopped: {reason}")
        _print_budgets(runner)
        if result.pending:
            print(f"Resume later with the same command: lab run {config_path.as_posix()}")
        else:
            print(f"All runs done. Report: lab report {config.name}")
        return 0
    finally:
        store.close()
        cache.close()


def cmd_label(labels_path: Path, store_path: Path) -> int:
    if not store_path.exists():
        print(f"No run store at {store_path}", file=sys.stderr)
        return 1
    labels = load_labels(labels_path)
    store = RunStore(store_path)
    try:
        written = apply_labels(labels, store)
    except ValueError as e:
        print(f"Label error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"Labelled {written} run(s) of {labels.experiment}.")
    return 0


def cmd_report(experiments: Sequence[str], store_path: Path, config_path: Path | None) -> int:
    if not store_path.exists():
        print(f"No run store at {store_path}", file=sys.stderr)
        return 1
    store = RunStore(store_path)
    try:
        records = [r for experiment in experiments for r in store.runs(experiment)]
        counts = [_expected_runs(experiment, config_path) for experiment in experiments]
        expected = None if None in counts else sum(c for c in counts if c is not None)
        print(report_text(
            ", ".join(experiments), compute_metrics(records), len(records), expected,
            flips=compute_flips(records),
            tasks={r.spec.user_task_id for r in records},
        ))
        return 0
    finally:
        store.close()


def report_text(
    experiment: str,
    groups: list[GroupMetrics],
    stored: int,
    expected: int | None,
    *,
    flips: Sequence[FlipMetrics] = (),
    tasks: Set[str] = frozenset(),
) -> str:
    """`tasks`: the user tasks the runs cover, to flag those with a known issue (D44)."""
    lines = [f"Experiment: {experiment}"]
    if expected is None:
        lines.append(f"Runs stored: {stored} (no config found to count pending runs)")
    else:
        lines.append(f"Runs stored: {stored} of {expected} ({expected - stored} pending, unscored)")
    if not groups:
        lines.append("No runs stored yet.")
    else:
        lines.append(
            "Discriminating tasks: those a do-nothing agent fails; excluded "
            f"{', '.join(sorted(DO_NOTHING_PASSES, key=_task_number))} (D37, D39)."
        )
    for task_id in sorted(tasks & KNOWN_ISSUES.keys(), key=_task_number):
        lines.append(f"Known issue, {task_id}: {KNOWN_ISSUES[task_id]} Scored unchanged.")
    for g in groups:
        errors = ", ".join(f"{k} {v}" for k, v in g.model_errors.items()) or "none"
        lines += [
            "",
            f"{g.provider}/{g.model_id}  topology {g.topology}",
            "  benign utility (95% Wilson CI)",
            f"    discriminating tasks  {_fmt_proportion(g.discriminating_utility)}  headline",
            f"    all tasks             {_fmt_proportion(g.benign_utility)}",
            f"    do-nothing floor      {_fmt_proportion(g.do_nothing_floor)}  same runs",
            "  outcomes         " + ", ".join(f"{k} {v}" for k, v in g.outcomes.items()),
            f"  model errors     {g.model_error_total}: {errors}",
            "  failure labels   "
            + (", ".join(f"{k} {v}" for k, v in g.failure_labels.items()) or "no failures"),
            f"  malformed calls  {g.malformed_tool_calls} in {g.runs_with_malformed_tool_calls} "
            f"run(s)",
            f"  calls            {g.calls} ({g.cached_calls} from cache), "
            f"{g.requests_sent} provider requests",
            f"  tokens           input {_fmt_int(g.input_tokens)}, output "
            f"{_fmt_int(g.output_tokens)} (reasoning {_fmt_reasoning(g)})",
            f"  est. cost        {_fmt_cost(g.cost_usd)} at public paid prices",
            f"  latency / call   {_fmt_latency(g.call_latency)}",
            f"  latency / task   {_fmt_latency(g.task_latency)}",
            f"  versions         agentdojo {', '.join(g.agentdojo_versions)}, benchmark "
            f"{', '.join(g.benchmark_versions)}, commit {', '.join(map(_short, g.git_commits))}",
            f"  seeds            {', '.join(map(str, g.seeds)) or 'none'}",
            f"  run dates (UTC)  {g.first_run_at} to {g.last_run_at}",
        ]
    for f in flips:
        if f.repeated:
            lines += ["", *_flip_lines(f)]
    return "\n".join(lines)


def _flip_lines(f: FlipMetrics) -> list[str]:
    """Each task's outcome on every repeat (1 solved, 0 not), and how often repeats disagree."""
    lines = [
        f"{f.provider}/{f.model_id}  topology {f.topology}: outcomes across repeats (D45)",
        f"  tasks that flip  {_fmt_proportion(f.tasks_flipped)}",
        "  flip rate        share of a task's repeat pairs whose outcomes differ",
        "  task            outcome by repeat  solved  flip rate",
    ]
    for t in f.tasks:
        outcomes = " ".join(f"r{idx}:{int(u)}" for idx, u in t.utilities)
        rate = "n/a" if t.flip_rate is None else f"{t.flip_rate:.2f}"
        mark = "  flips" if t.flipped else ""
        lines.append(
            f"  {t.user_task_id:<15} {outcomes:<18} {t.solved}/{len(t.utilities)}     {rate}{mark}"
        )
    return lines


def _expected_runs(experiment: str, config_path: Path | None) -> int | None:
    path = config_path or DEFAULT_EXPERIMENTS_DIR / f"{experiment}.yaml"
    if not path.exists():
        return None
    config: ExperimentConfig = load_experiment(path)
    if config.name != experiment:
        return None
    return len({s.run_id for s in expand(config, load_providers())})


def _environ(env_file: Path) -> dict[str, str]:
    """The real environment, with .env values filling in what it lacks. Never printed."""
    values = {k: v for k, v in dotenv_values(env_file).items() if v} if env_file.exists() else {}
    return {**values, **os.environ}


def _print_budgets(runner: ExperimentRunner) -> None:
    for ref in runner.config.models:
        budget: TokenBudget = runner.token_budget(ref)
        limit = " (no daily limit)" if budget.limit is None else f" of {budget.limit:,}"
        print(f"  {model_key(ref.provider, ref.model_id)}: {budget.used:,} tokens used in the "
              f"last 24 h{limit}; {_fmt_tokens(budget.remaining)} left")


def _short(commit: str) -> str:
    head, dirty, _ = commit.partition("-dirty")
    return head[:12] + dirty


def _fmt_tokens(value: int | None) -> str:
    return "unlimited" if value is None else f"{value:,}"


def _fmt_int(value: int | None) -> str:
    return "unknown" if value is None else f"{value:,}"


def _fmt_reasoning(g: GroupMetrics) -> str:
    """The total if every call reported it; otherwise the range the reported counts allow."""
    if g.reasoning_tokens is not None or g.reasoning_tokens_range is None:
        return _fmt_int(g.reasoning_tokens)
    low, high = g.reasoning_tokens_range
    return (
        f"{low:,} to {high:,}; {g.calls_without_reasoning_tokens} of {g.calls} calls "
        "had no count (D40)"
    )


def _fmt_proportion(p: Proportion) -> str:
    low, high = p.ci95
    rate = "n/a" if p.rate is None else f"{p.rate:.1%}"
    return f"{p.successes}/{p.n} = {rate}  ({low:.1%} to {high:.1%})"


def _task_number(task_id: str) -> int:
    return int(task_id.rsplit("_", 1)[1])


def _fmt_cost(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.4f}"


def _fmt_latency(latency: Latency) -> str:
    if latency.p50 is None or latency.p95 is None:
        return "no uncached samples"
    return f"p50 {latency.p50:.2f} s, p95 {latency.p95:.2f} s (n={latency.n}, cache hits excluded)"


if __name__ == "__main__":
    sys.exit(main())
