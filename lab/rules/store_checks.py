"""`lab validate-rules --store`, group B: the checks that read the run store (PLAN_M3 §5, D102,
D117). Local only: the store is not committed, so CI cannot run them.

| Check | Fails when |
|---|---|
| B1 D74 confusion table | `ended_with_question` / `requested_user_input`, against the hand |
|  | annotation, no longer give D74's counts on the baseline runs |
| B2 D88 summary parser | summary_parser_v1 no longer agrees with D88's 10 hand verdicts |
| B3 no test run before the prereg | a test-split case has a run that started before the commit |
|  | that added PREREGISTRATION.md, or no such commit exists |
| B4 prereg version of M4 runs | a test-split run, or a benign control of a pinned experiment, |
|  | lacks the committed prereg version in force when it started |
| B5 judge registration | the judge scored a test run before it was registered, or kappa < 0.6 |
| B6 blinding | `lab report` / `lab trace` showed test-screen or test-confirm outcomes |
|  | before the valid unblinding record of that set |

"M4 run" (B4) means: a run of a test-split case, or a benign run whose config is pinned in the
prereg (the benign controls have no case). The guard stamps the same set (lab.rules.guard).
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from lab.rules.checks import Check
from lab.rules.golden import Golden, GoldenError, load_golden
from lab.rules.prereg import (
    JUDGE_MIN_KAPPA,
    PREREG_NAME,
    PreregError,
    blinding_state,
    load_freeze_log,
    load_pins,
)
from lab.rules.scope import SplitData, case_id_of, load_split_data
from lab.store import RunIndexRow, RunStore

EXPERIMENTS_DIR = "configs/experiments/"


def _time(iso: str) -> datetime:
    parsed = datetime.fromisoformat(iso)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True)
class PreregVersion:
    """One committed version of PREREGISTRATION.md: when it was committed and its raw-bytes
    SHA-256, which is what `prereg_sha256` on a run holds."""

    committed_at: datetime
    sha256: str


def git_prereg_versions(root: Path) -> list[PreregVersion]:
    """Every committed version of PREREGISTRATION.md, oldest first. Empty if it was never
    committed (or there is no git)."""
    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              check=True).stdout

    try:
        log = git("log", "--reverse", "--format=%H %cI", "--", PREREG_NAME).decode().splitlines()
        versions = []
        for line in log:
            commit, committed_at = line.split()
            try:
                content = git("show", f"{commit}:{PREREG_NAME}")
            except subprocess.CalledProcessError:
                continue  # a commit that deleted it
            versions.append(PreregVersion(_time(committed_at), hashlib.sha256(content).hexdigest()))
    except (OSError, subprocess.CalledProcessError):
        return []
    return sorted(versions, key=lambda v: v.committed_at)  # stable: ties keep git's order


# --- B1, B2: the hand-validated rules (D89) ----------------------------------------------------


def check_d74(store: RunStore, golden: Golden) -> Check:
    from lab.scoring.attack_metrics import check_rule, ended_with_question, requested_user_input

    name = "B1 D74 confusion table"
    recheck = golden.rechecks.d74
    records = [r for experiment in recheck.experiments for r in store.runs(experiment)]
    rules: dict[str, Callable[..., bool]] = {
        "ended_with_question": ended_with_question,
        "requested_user_input": requested_user_input,
    }
    problems = []
    if not any(r.ended_asking_user is not None for r in records):
        problems.append(f"no annotated run in {', '.join(recheck.experiments)}: store the "
                        "baseline runs and apply configs/labels/ with `lab label`")
    for rule_name, frozen in recheck.tables.items():
        rule = rules.get(rule_name)
        if rule is None:
            problems.append(f"{rule_name}: not a rule this check knows")
            continue
        table = check_rule(records, rule)
        got = [table.true_positives, table.false_positives, table.false_negatives,
               table.true_negatives]
        if got != frozen:
            problems.append(f"{rule_name}: TP/FP/FN/TN is {got}, D74 has {frozen}")
    return Check(name, tuple(problems))


def check_d88(root: Path, golden: Golden) -> Check:
    from lab.defenses.human import intent_spec, load_summary_parser, read_summary
    from lab.domains.agentdojo_bridge import load_banking_suite

    name = "B2 D88 summary parser against the hand verdicts"
    recheck = golden.rechecks.d88
    path = root / recheck.file
    if not path.is_file():
        return Check(name, (f"{recheck.file} does not exist",))
    summaries = yaml.safe_load(path.read_text(encoding="utf-8"))["summaries"]
    parser, suite = load_summary_parser(), load_banking_suite()
    agree, problems = 0, []
    for item in summaries:
        reading = read_summary(parser, item["summary"], intent_spec(suite, item["user_task_id"]))
        if reading.approve == (item["hand_verdict"] == "approve"):
            agree += 1
        else:
            problems.append(f"{item['run_id'][:12]}: the parser says "
                            f"{'approve' if reading.approve else 'reject'}, the hand verdict is "
                            f"{item['hand_verdict']}")
    if len(summaries) != recheck.n or agree != recheck.agree:
        problems.append(f"{agree}/{len(summaries)} agree, D88 has {recheck.agree}/{recheck.n}")
    return Check(name, tuple(problems))


# --- B3 to B6: the registration order ------------------------------------------------------------


def _is_test(row: RunIndexRow, data: SplitData) -> bool:
    return data.is_test(case_id_of(row.case_id, row.user_task_id, row.injection_task_id))


def check_no_test_before_prereg(
    index: list[RunIndexRow], data: SplitData, versions: list[PreregVersion]
) -> Check:
    name = "B3 no test-case run before the prereg commit"
    test_runs = [r for r in index if _is_test(r, data)]
    if not test_runs:
        return Check(name)
    committed_at = versions[0].committed_at if versions else None
    if committed_at is None:
        return Check(name, (f"{len(test_runs)} test-case run(s) are stored, but no commit adds "
                            f"{PREREG_NAME}",))
    early = [r for r in test_runs if _time(r.started_at) < committed_at]
    return Check(name, tuple(
        f"{r.run_id[:12]} ({r.experiment}) started {r.started_at}, before the prereg commit of "
        f"{committed_at.isoformat()}" for r in early
    ))


def pinned_experiment_hashes(root: Path) -> set[str]:
    """Config hashes of the experiment configs the prereg pins."""
    from lab.config import load_experiment

    try:
        pins = load_pins(root)
    except PreregError:
        return set()
    return {
        load_experiment(root / path).config_hash
        for path in pins if path.startswith(EXPERIMENTS_DIR) and (root / path).is_file()
    }


def is_m4_run(row: RunIndexRow, data: SplitData, pinned_hashes: set[str]) -> bool:
    benign = row.case_id is None and row.injection_task_id is None
    return _is_test(row, data) or (benign and row.config_hash in pinned_hashes)


def version_in_force(versions: list[PreregVersion], at: datetime) -> PreregVersion | None:
    """The latest committed version at or before `at`."""
    earlier = [v for v in versions if v.committed_at <= at]
    return earlier[-1] if earlier else None


def check_prereg_hash(
    root: Path, index: list[RunIndexRow], data: SplitData, versions: list[PreregVersion]
) -> Check:
    """Each M4 run's `prereg_sha256` is a committed version of PREREGISTRATION.md, and the one in
    force when the run started (the latest committed at or before its start). An amendment adds a
    version; it does not invalidate the runs made under the version before it."""
    name = "B4 every M4 run carries the prereg version in force when it started"
    pinned = pinned_experiment_hashes(root)
    m4 = [r for r in index if is_m4_run(r, data, pinned)]
    problems = []
    for r in m4:
        label = f"{r.run_id[:12]} ({r.experiment})"
        if r.prereg_sha256 is None:
            problems.append(f"{label}: prereg_sha256 is missing")
            continue
        in_force = version_in_force(versions, _time(r.started_at))
        if in_force is None:
            problems.append(f"{label}: started {r.started_at}, before any committed version of "
                            f"{PREREG_NAME}")
        elif r.prereg_sha256 == in_force.sha256:
            continue
        elif r.prereg_sha256 in {v.sha256 for v in versions}:
            problems.append(f"{label}: carries a committed version of the prereg, but not the one "
                            f"in force at its start ({in_force.committed_at.isoformat()})")
        else:
            problems.append(f"{label}: prereg_sha256 {r.prereg_sha256[:12]} is not a committed "
                            f"version of {PREREG_NAME}")
    return Check(name, tuple(problems))


def check_judge(
    root: Path, store: RunStore, index: list[RunIndexRow], data: SplitData
) -> Check:
    name = "B5 no judge verdict on a test case before the judge's registration"
    by_id = {r.run_id: r for r in index}
    scored = [(run_id, at) for run_id, at in store.judge_verdicts()
              if run_id in by_id and _is_test(by_id[run_id], data)]
    if not scored:
        return Check(name)
    try:
        log = load_freeze_log(root)
    except PreregError as error:
        return Check(name, (str(error),))
    judge = log.judge if log is not None else None
    if judge is None:
        return Check(name, (f"{len(scored)} test-case verdict(s) are stored, but the freeze log "
                            "has no judge registration",))
    problems = []
    if judge.kappa < JUDGE_MIN_KAPPA:
        problems.append(f"the judge is registered with kappa {judge.kappa}, below "
                        f"{JUDGE_MIN_KAPPA}")
    problems += [f"{run_id[:12]}: scored {at}, before the registration of "
                 f"{judge.registered_at.isoformat()}"
                 for run_id, at in scored if _time(at) < judge.registered_at]
    return Check(name, tuple(problems))


def check_blinding(root: Path, store: RunStore) -> Check:
    """No view showed the outcomes of a blinded set (test-screen or test-confirm) before that
    set's valid unblinding record."""
    name = "B6 no unblinded test outcome before its unblinding record"
    problems = []
    for view in (v for v in store.outcome_views() if v.runs > 0):
        state = blinding_state(root, "test_confirm" if view.scope == "test_confirm"
                               else "test_screen")
        what = (f"`lab {view.command} {view.detail}` showed {view.runs} {view.scope} run(s) "
                f"at {view.viewed_at}")
        if not state.unblinded or state.recorded_at is None:
            problems.append(f"{what}, but {state.reason}")
        elif _time(view.viewed_at) < state.recorded_at:
            problems.append(f"{what}, before the unblinding record of "
                            f"{state.recorded_at.isoformat()}")
    return Check(name, tuple(problems))


def run_group_b(
    root: Path,
    store: RunStore,
    prereg_versions: Callable[[Path], list[PreregVersion]] = git_prereg_versions,
) -> list[Check]:
    """Every group B check. `prereg_versions` lists the committed versions of
    PREREGISTRATION.md, oldest first (none: it was never committed); by default it asks git."""
    checks: list[Check] = []
    try:
        golden = load_golden(root)
        checks += [check_d74(store, golden), check_d88(root, golden)]
    except GoldenError as error:
        checks += [Check("B1 D74 confusion table", (str(error),)),
                   Check("B2 D88 summary parser against the hand verdicts", (str(error),))]
    try:
        data = load_split_data(root)
    except (OSError, ValueError, KeyError) as error:
        return [*checks, Check("B3 to B5", (f"the split cannot be read: {error}",)),
                check_blinding(root, store)]
    index = store.run_index()
    versions = prereg_versions(root)
    checks += [
        check_no_test_before_prereg(index, data, versions),
        check_prereg_hash(root, index, data, versions),
        check_judge(root, store, index, data),
        check_blinding(root, store),
    ]
    return checks
