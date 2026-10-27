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
from collections.abc import Mapping, Sequence, Set
from pathlib import Path

from dotenv import dotenv_values

from lab.config import ExperimentConfig, expand, load_experiment
from lab.defenses.human import DUPLICATES_NOT_CAUGHT, PARSER_V1_LIMIT, PARSER_V1_LIMIT_TASK
from lab.domains.agentdojo_bridge import DO_NOTHING_PASSES, KNOWN_ISSUES
from lab.errors import ConfigError
from lab.models.cache import ResponseCache
from lab.models.providers import load_providers
from lab.runner import ExperimentRunner, TokenBudget, model_key
from lab.scheduler.clock import SystemClock
from lab.scheduler.spec import RunSpec
from lab.scoring.attack_metrics import (
    MIN_CASES_FOR_A_RESULT,
    CaseMean,
    OversightMetrics,
    compute_oversight,
)
from lab.scoring.failure_labels import apply_labels, load_labels
from lab.scoring.metrics import (
    FlipMetrics,
    GroupMetrics,
    Latency,
    Proportion,
    compute_flips,
    compute_metrics,
)
from lab.store import PrefixDivergence, RunRecord, RunStore, prefix_divergence
from lab.topologies import presets
from lab.tracing.render import ascii_safe, trace_text

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

    trace = commands.add_parser(
        "trace", help="print one run's multi-agent trace, with its marker hits and hops"
    )
    trace.add_argument("run_id", help="a run ID, or a unique prefix of one")
    trace.add_argument("--store", type=Path, default=DEFAULT_STORE)

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args.config, args.store, args.cache, args.env_file)
        if args.command == "trace":
            return cmd_trace(args.run_id, args.store)
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
                  f"{spec.user_task_id} {_case_kind(spec)} {spec.topology} r{spec.repeat_idx}: "
                  f"{event}  (tokens left today: {left})", flush=True)

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


def _case_kind(spec: RunSpec) -> str:
    """For the progress lines: benign, or attack:<injection task> for an attacked run."""
    return "benign" if spec.injection_task_id is None else f"attack:{spec.injection_task_id}"


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


def cmd_trace(run_id: str, store_path: Path) -> int:
    if not store_path.exists():
        print(f"No run store at {store_path}", file=sys.stderr)
        return 1
    store = RunStore(store_path)
    try:
        found = store.run_ids_with_prefix(run_id)
        if len(found) != 1:
            print(f"{len(found)} stored runs have an ID starting with {run_id!r}",
                  file=sys.stderr)
            return 1
        record = store.get_run(found[0])
        print(ascii_safe(trace_text(record, store.get_prefix_divergence(record.run_id))))
        return 0
    finally:
        store.close()


def cmd_report(experiments: Sequence[str], store_path: Path, config_path: Path | None) -> int:
    if not store_path.exists():
        print(f"No run store at {store_path}", file=sys.stderr)
        return 1
    store = RunStore(store_path)
    try:
        records = [r for experiment in experiments for r in store.runs(experiment)]
        prices = {
            (p.name, model_id): info.price
            for p in load_providers().values()
            for model_id, info in p.models.items()
        }
        counts = [_expected_runs(experiment, config_path) for experiment in experiments]
        expected = None if None in counts else sum(c for c in counts if c is not None)
        # Benign utility is computed on benign runs only. An attacked run's utility means
        # something else (solved under attack), and pooling the two would move the baseline.
        benign = [r for r in records if r.spec.attack is None]
        print(report_text(
            ", ".join(experiments), compute_metrics(benign, prices=prices), len(records), expected,
            flips=compute_flips(benign),
            tasks={r.spec.user_task_id for r in benign},
            notes=known_limit_notes(benign),
        ))
        if any(r.spec.topology != "t1" or r.spec.attack is not None for r in records):
            attacked = [r for r in records if r.spec.attack is not None]
            print()
            print(oversight_text(
                compute_oversight(records),
                volume={(g.provider, g.model_id, g.topology): g
                        for g in compute_metrics(attacked, prices=prices)},
                divergences=check_divergences(store, records),
            ))
        return 0
    finally:
        store.close()


def known_limit_notes(benign: Sequence[RunRecord]) -> dict[tuple[str, str, str], list[str]]:
    """Notes for the benign groups a known limit affects (D71): T5-summary's utility on
    user_task_11, shown with and without that task, and how many of its summaries v1 rejected
    for naming an amount."""
    notes: dict[tuple[str, str, str], list[str]] = {}
    for g in compute_metrics(r for r in benign if r.spec.topology == "t5_summary"):
        runs = [r for r in benign if (r.spec.provider, r.spec.model_id, r.spec.topology)
                == (g.provider, g.model_id, g.topology)]
        limited = [r for r in runs if r.spec.user_task_id == PARSER_V1_LIMIT_TASK]
        if not limited:
            continue
        rest = [r for r in runs if r.spec.user_task_id != PARSER_V1_LIMIT_TASK]
        rejected = sum(
            any(e.kind == "approval_decision"
                and (e.payload["reading"] or {}).get("step") == "amount_out_of_bounds"
                for e in r.events)
            for r in limited
        )
        notes[(g.provider, g.model_id, g.topology)] = [
            f"  known limit      {PARSER_V1_LIMIT}",
            f"    {PARSER_V1_LIMIT_TASK}: solved {sum(r.utility for r in limited)}/"
            f"{len(limited)}; a summary rejected as amount_out_of_bounds in {rejected} run(s)",
            f"    all tasks without {PARSER_V1_LIMIT_TASK}  "
            f"{_fmt_proportion(Proportion(sum(r.utility for r in rest), len(rest)))}",
        ]
    return notes


def check_divergences(
    store: RunStore, records: Sequence[RunRecord]
) -> dict[tuple[str, str, str, bool], list[PrefixDivergence]]:
    """Every stored prefix_divergence row of these runs, recomputed from the stored request keys
    with the same function (D66). Raises if a row disagrees, or if a child and its parent run
    are both stored and the row is missing: the report must not print numbers it can't
    reproduce."""
    parents = presets.parents()
    out: dict[tuple[str, str, str, bool], list[PrefixDivergence]] = {}
    for r in records:
        if r.spec.topology not in parents:
            continue
        parent = r.spec.model_copy(update={"topology": parents[r.spec.topology]})
        row = store.get_prefix_divergence(r.run_id)
        if row is None:
            if store.has_run(parent.run_id):
                raise RuntimeError(f"Run {r.run_id} and its parent are stored, but its "
                                   "prefix_divergence row is missing")
            continue
        recomputed = prefix_divergence(
            r.run_id, parent.run_id, store.request_keys(r.run_id),
            store.request_keys(parent.run_id),
        )
        if recomputed != row:
            raise RuntimeError(f"Stored prefix_divergence of {r.run_id} is {row}, but the "
                               f"request keys give {recomputed}")
        key = (r.spec.provider, r.spec.model_id, r.spec.topology, r.spec.attack is not None)
        out.setdefault(key, []).append(row)
    return out


def oversight_text(
    groups: Sequence[OversightMetrics],
    *,
    volume: Mapping[tuple[str, str, str], GroupMetrics],
    divergences: Mapping[tuple[str, str, str, bool], list[PrefixDivergence]],
) -> str:
    """The attack and oversight section (D69): attacked runs first, then the benign controls'
    oversight numbers (gate decisions, handoffs, marker false positives)."""
    lines = ["Attacks and oversight (PLAN_M2 §5, §6, §9, §10)",
             "  ASR = attack succeeded / all stored attack runs, model errors included (D49)."]
    if any(g.topology == "t5" for g in groups):
        lines.append(f"  Known limit, t5 and t5_summary: {DUPLICATES_NOT_CAUGHT}")
    for g in groups:
        key = (g.provider, g.model_id, g.topology)
        kind = "attack runs" if g.attacked else "benign controls"
        lines += ["", f"{g.provider}/{g.model_id}  topology {g.topology}  {kind}: n = {g.n} "
                      f"over {g.n_cases} case(s)"]
        if g.n_cases < MIN_CASES_FOR_A_RESULT:
            lines.append(f"  plumbing check, n = {g.n}, not a result (fewer than "
                         f"{MIN_CASES_FOR_A_RESULT} cases)")
        if g.attacked:
            assert g.asr is not None and g.utility_under_attack is not None
            assert g.asr_by_case is not None and g.utility_under_attack_by_case is not None
            lines += [
                f"  ASR                    {_fmt_proportion(g.asr)}",
                f"  utility under attack   {_fmt_proportion(g.utility_under_attack)}",
                f"  per case over repeats  ASR {_fmt_case_mean(g.asr_by_case)}",
                f"                         utility under attack "
                f"{_fmt_case_mean(g.utility_under_attack_by_case)}",
            ]
        else:
            lines.append(f"  solved                 {_fmt_proportion(g.solved)}")
        lines += _table_lines(g)
        lines += _marker_lines(g)
        lines += _oversight_lines(g)
        flips = g.cases_flipped
        outcome = "attack outcome" if g.attacked else "solved"
        rate = "n/a" if g.mean_flip_rate is None else f"{g.mean_flip_rate:.2f}"
        lines.append(f"  flips over repeats     {outcome}: cases that flip {flips.successes}/"
                     f"{flips.n}, mean flip rate {rate}")
        if g.attacked and key in volume:
            v = volume[key]
            lines += [
                f"  calls                  {v.calls} ({v.cached_calls} from cache), "
                f"{v.requests_sent} provider requests",
                f"  tokens                 input {_fmt_int(v.input_tokens)}, output "
                f"{_fmt_int(v.output_tokens)} (reasoning {_fmt_reasoning(v)})",
                f"  est. cost              {_fmt_cost(v.cost_usd)} at public paid prices",
                *_usage_lines(v, "  "),
                f"  latency / call         {_fmt_latency(v.call_latency)}",
                f"  latency / task         {_fmt_latency(v.task_latency)}",
            ]
        rows = divergences.get((*key, g.attacked), [])
        if rows:
            diverged = [r for r in rows if r.divergence_call_idx is not None]
            shared = sum(r.n_shared_calls for r in rows) / len(rows)
            parent = presets.parents()[g.topology]
            lines.append(f"  prefix vs {parent:<12} {len(diverged)}/{len(rows)} diverged, "
                         f"{shared:.1f} shared calls on average (recomputed, D66)")
    return "\n".join(lines)


def _table_lines(g: OversightMetrics) -> list[str]:
    """The 2x2 of solved x attack_succeeded, with the safe-unsolved cell broken down (§9)."""
    t = g.table
    s = g.safe_unsolved
    asked = (f"{s['asked']}" if s["asked"] == g.asked_upper
             else f"{s['asked']} to {g.asked_upper}")  # lower to upper bound (D74)
    unsolved = (f"{t.get((False, False), 0)} (disclosed {s['disclosed']}, asked "
                f"{asked}, other {s['other']}{'' if s['asked'] == g.asked_upper else ' or fewer'})")
    if not g.attacked:
        return [f"  solved x attack        solved {t.get((True, False), 0)}, unsolved "
                f"{unsolved}"]
    return [
        "  solved x attack        attack failed    attack succeeded",
        f"    solved               {t.get((True, False), 0):<16} {t.get((True, True), 0)}",
        f"    not solved           {unsolved}  {t.get((False, True), 0)}",
    ]


def _marker_lines(g: OversightMetrics) -> list[str]:
    if not g.marker_runs:
        return ["  markers                not searched"]
    if not g.attacked:
        return [f"  benign marker hits     {g.runs_with_hits}/{g.marker_runs} runs (false "
                "positives: every goal marker is searched, D52)"]
    depths = ", ".join(
        f"{'none' if d is None else d}: {n}" for d, n in g.depths.items()
    )
    lines = [
        f"  propagated             {_fmt_proportion(g.propagated)}  (agents' own path)",
        f"  infection depth        runs by depth: {depths}",
        f"  acted on               {_fmt_proportion(g.acted_on)}",
        f"  gate leak              {_fmt_proportion(g.gate_leak)}  (a gate's notice was an "
        "agent's only source, D67)",
        f"  gate leak acted on     {_fmt_proportion(g.gate_leak_acted_on)}  (a leaked marker "
        "later in a tool call's args, D72)",
        f"  reviewer exposed       {_fmt_proportion(g.reviewer_exposed)}",
    ]
    if g.by_role:
        lines.append("  by author role         " + "; ".join(
            f"{role}/{fld} {h.runs} run(s), max hop {'none' if h.max_hop is None else h.max_hop}"
            for (role, fld), h in g.by_role.items()
        ))
    return lines


def _oversight_lines(g: OversightMetrics) -> list[str]:
    decisions = ", ".join(f"{k} {v}" for k, v in g.gate_decisions.items()) or "none"
    lines = [
        f"  gate decisions         {decisions}",
        f"  handoffs               {g.handoffs} ({g.handoffs / g.n:.1f} per run)",
        f"  ended with a question  {g.ended_with_question}  (lower bound, D73)",
        f"  requested user input   {g.requested_user_input}  (upper bound, D74)",
    ]
    if g.approvals_requested:
        lines.append(f"  approvals requested    {g.approvals_requested} "
                     f"({g.approvals_requested / g.n:.1f} per run)")
    if g.summaries:
        lines.append(f"  summaries unparseable  {_fmt_proportion(g.summary_unparseable)}")
    return lines


def _fmt_case_mean(c: CaseMean) -> str:
    if c.mean is None or c.ci95 is None:
        return "n/a"
    low, high = c.ci95
    return (f"{c.mean:.1%}  (cluster bootstrap 95% CI {low:.1%} to {high:.1%}, "
            f"{c.n_cases} case(s), {c.n_runs} run(s))")


def report_text(
    experiment: str,
    groups: list[GroupMetrics],
    stored: int,
    expected: int | None,
    *,
    flips: Sequence[FlipMetrics] = (),
    tasks: Set[str] = frozenset(),
    notes: Mapping[tuple[str, str, str], list[str]] | None = None,
) -> str:
    """`tasks`: the user tasks the runs cover, to flag those with a known issue (D44).
    `notes`: extra lines for a (provider, model, topology) group: a known limit (D71)."""
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
            *_out_of_scope_line(g),
            f"  calls            {g.calls} ({g.cached_calls} from cache), "
            f"{g.requests_sent} provider requests",
            f"  tokens           input {_fmt_int(g.input_tokens)}, output "
            f"{_fmt_int(g.output_tokens)} (reasoning {_fmt_reasoning(g)})",
            f"  est. cost        {_fmt_cost(g.cost_usd)} at public paid prices",
            *_usage_lines(g, "  "),
            f"  latency / call   {_fmt_latency(g.call_latency)}",
            f"  latency / task   {_fmt_latency(g.task_latency)}",
            f"  versions         agentdojo {', '.join(g.agentdojo_versions)}, benchmark "
            f"{', '.join(g.benchmark_versions)}, commit {', '.join(map(_short, g.git_commits))}",
            f"  seeds            {', '.join(map(str, g.seeds)) or 'none'}",
            f"  run dates (UTC)  {g.first_run_at} to {g.last_run_at}",
        ]
        lines += (notes or {}).get((g.provider, g.model_id, g.topology), [])
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


def _out_of_scope_line(g: GroupMetrics) -> list[str]:
    """Calls to tools the agent doesn't have (D76). No line for groups stored before D76, which
    didn't count them, so their reports are unchanged."""
    if g.out_of_scope_tool_calls is None:
        return []
    return [f"  out-of-scope     {g.out_of_scope_tool_calls} call(s) in "
            f"{g.runs_with_out_of_scope_tool_calls} run(s), never run"]


def _usage_lines(g: GroupMetrics, pad: str) -> list[str]:
    """Known tokens and cost, the calls with no usage, and the estimated range (D79). Nothing
    when every call reported usage, so those reports are unchanged. The totals above stay
    unknown (D25); these lines sit beside them and never replace them."""
    if g.calls_without_usage == 0:
        return []
    lines = [
        f"{pad}known usage       {g.known_tokens:,} tokens, ${g.known_cost_usd:.4f} "
        f"({g.calls - g.calls_without_usage} of {g.calls} calls)",
        f"{pad}no usage          {g.calls_without_usage} of {g.calls} calls (out-of-scope "
        "attempts, D76)",
    ]
    if g.tokens_estimate_high is None:
        return lines + [f"{pad}estimated range   unavailable: a call has no stored estimate"]
    cost = "" if g.cost_estimate_high_usd is None else f", ${g.cost_estimate_high_usd:.4f}"
    return lines + [
        f"{pad}ESTIMATED range   {g.known_tokens:,} to {g.tokens_estimate_high:,} tokens"
        f"{cost}: known plus the limiter's pre-send estimate for those calls, not measured (D79)"
    ]


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
