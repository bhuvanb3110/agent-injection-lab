"""`lab run <experiment.yaml>` and `lab report <experiment>` (D35).

`lab run` is resumable: runs already in the store are skipped, so after a daily cap the same
command, run again once the 24 h window has room, carries on where it stopped.

Secrets: keys are read from `.env` (or the real environment) into a dict handed to the client.
They are never printed, and never put in os.environ, a cache key, a trace or a run record.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values

from lab.config import ExperimentConfig, expand, load_experiment
from lab.errors import ConfigError
from lab.models.cache import ResponseCache
from lab.models.providers import load_providers
from lab.runner import ExperimentRunner, TokenBudget, model_key
from lab.scheduler.clock import SystemClock
from lab.scheduler.spec import RunSpec
from lab.scoring.metrics import GroupMetrics, Latency, compute_metrics
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

    report = commands.add_parser("report", help="metrics for an experiment from the run store")
    report.add_argument("experiment", help="the experiment's name (its config's `name`)")
    report.add_argument("--store", type=Path, default=DEFAULT_STORE)
    report.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config to count pending runs against (default: configs/experiments/<name>.yaml)",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args.config, args.store, args.cache, args.env_file)
        return cmd_report(args.experiment, args.store, args.config)
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


def cmd_report(experiment: str, store_path: Path, config_path: Path | None) -> int:
    if not store_path.exists():
        print(f"No run store at {store_path}", file=sys.stderr)
        return 1
    store = RunStore(store_path)
    try:
        records = store.runs(experiment)
        expected = _expected_runs(experiment, config_path)
        print(report_text(experiment, compute_metrics(records), len(records), expected))
        return 0
    finally:
        store.close()


def report_text(
    experiment: str, groups: list[GroupMetrics], stored: int, expected: int | None
) -> str:
    lines = [f"Experiment: {experiment}"]
    if expected is None:
        lines.append(f"Runs stored: {stored} (no config found to count pending runs)")
    else:
        lines.append(f"Runs stored: {stored} of {expected} ({expected - stored} pending, unscored)")
    if not groups:
        lines.append("No runs stored yet.")
    for g in groups:
        u = g.benign_utility
        low, high = u.ci95
        rate = "n/a" if u.rate is None else f"{u.rate:.1%}"
        errors = ", ".join(f"{k} {v}" for k, v in g.model_errors.items()) or "none"
        lines += [
            "",
            f"{g.provider}/{g.model_id}  topology {g.topology}",
            f"  benign utility   {u.successes}/{u.n} = {rate}  (95% Wilson CI {low:.1%} to "
            f"{high:.1%})",
            "  outcomes         " + ", ".join(f"{k} {v}" for k, v in g.outcomes.items()),
            f"  model errors     {g.model_error_total}: {errors}",
            f"  malformed calls  {g.malformed_tool_calls} in {g.runs_with_malformed_tool_calls} "
            f"run(s)",
            f"  calls            {g.calls} ({g.cached_calls} from cache), "
            f"{g.requests_sent} provider requests",
            f"  tokens           input {_fmt_int(g.input_tokens)}, output "
            f"{_fmt_int(g.output_tokens)} (reasoning {_fmt_int(g.reasoning_tokens)})",
            f"  est. cost        {_fmt_cost(g.cost_usd)} at public paid prices",
            f"  latency / call   {_fmt_latency(g.call_latency)}",
            f"  latency / task   {_fmt_latency(g.task_latency)}",
            f"  versions         agentdojo {', '.join(g.agentdojo_versions)}, benchmark "
            f"{', '.join(g.benchmark_versions)}, commit {', '.join(map(_short, g.git_commits))}",
            f"  seeds            {', '.join(map(str, g.seeds)) or 'none'}",
            f"  run dates (UTC)  {g.first_run_at} to {g.last_run_at}",
        ]
    return "\n".join(lines)


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


def _fmt_cost(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.4f}"


def _fmt_latency(latency: Latency) -> str:
    if latency.p50 is None or latency.p95 is None:
        return "no uncached samples"
    return f"p50 {latency.p50:.2f} s, p95 {latency.p95:.2f} s (n={latency.n}, cache hits excluded)"


if __name__ == "__main__":
    sys.exit(main())
