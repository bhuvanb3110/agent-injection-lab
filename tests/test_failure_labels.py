"""Hand failure labels: the committed baseline file, and writing labels into the store (D43)."""

from pathlib import Path

import pytest

from lab.domains.agentdojo_bridge import load_banking_suite, run_case
from lab.domains.agentdojo_bridge.run_case import CaseResult
from lab.domains.agentdojo_bridge.scripted import script_for
from lab.models.fake import FakeModel, Step, answer, call
from lab.models.llm_element import LabLLM
from lab.models.types import RunContext
from lab.scheduler.clock import FakeClock
from lab.scheduler.queue import Provenance, Scheduler
from lab.scheduler.spec import RunSpec
from lab.scoring.failure_labels import (
    FailureLabel,
    LabelEntry,
    LabelFile,
    apply_labels,
    automatic_label,
    load_labels,
)
from lab.store import RunRecord, RunStore
from lab.topologies.t2 import WORKER_MAX_ITERS, build_t2

LABELS = Path(__file__).resolve().parents[1] / "configs" / "labels"
M120, M20 = "openai/gpt-oss-120b", "openai/gpt-oss-20b"


def test_baseline_labels_split_asked_user_from_gave_up_early() -> None:
    labels = load_labels(LABELS / "baseline_banking.yaml")
    by_run = {(e.model_id, e.user_task_id, e.repeat_idx): e.label for e in labels.labels}
    # D41's three "gave up early" runs all asked the user for something their tools held.
    asked = {k for k, v in by_run.items() if v == FailureLabel.ASKED_USER}
    assert asked == {(M120, "user_task_4", 0), (M20, "user_task_0", 0), (M20, "user_task_11", 0)}
    assert FailureLabel.GAVE_UP_EARLY not in by_run.values()
    # Every not-solved baseline run (D41) has exactly one label.
    assert set(by_run) == asked | {
        (M120, "user_task_1", 0), (M120, "user_task_3", 0), (M120, "user_task_9", 0)
    }
    assert len(by_run) == len(labels.labels)


def run(task: str, solved: bool) -> RunRecord:
    spec = RunSpec(
        experiment="e", suite="banking", benchmark_version="v1.2.2", user_task_id=task,
        topology="t1", provider="groq", model_id=M20, temperature=0.6,
        reasoning_effort="medium", max_tokens=2048, seed=1, repeat_idx=0,
    )
    return RunRecord(
        run_id=spec.run_id, spec=spec, config_hash="c", git_commit="g", agentdojo_version="v",
        seed=1, outcome="solved" if solved else "not_solved", outcome_subtype=None,
        utility=solved, state_check_passed=solved, error=None, calls=[], malformed_tool_calls=0,
        messages=[], started_at="t", finished_at="t", wall_latency_s=0.0,
    )


def entry(task: str) -> LabelEntry:
    return LabelEntry(provider="groq", model_id=M20, user_task_id=task, repeat_idx=0,
                      label=FailureLabel.ASKED_USER)


def test_apply_writes_labels_and_rejects_solved_or_missing_runs(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    failed, solved = run("user_task_0", False), run("user_task_1", True)
    store.save_run(failed)
    store.save_run(solved)

    assert apply_labels(LabelFile(experiment="e", labels=[entry("user_task_0")]), store) == 1
    assert store.get_run(failed.run_id).failure_label == "asked_user"
    assert store.get_run(failed.run_id).utility is False  # a label never changes a score

    with pytest.raises(ValueError, match="was solved"):
        apply_labels(LabelFile(experiment="e", labels=[entry("user_task_1")]), store)
    with pytest.raises(ValueError, match="No stored run"):
        apply_labels(LabelFile(experiment="e", labels=[entry("user_task_2")]), store)


def test_apply_writes_ended_asking_user_only_when_the_entry_has_it(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    asked, plain = run("user_task_0", False), run("user_task_1", False)
    store.save_run(asked)
    store.save_run(plain)
    entries = [
        entry("user_task_0").model_copy(update={"ended_asking_user": True}),
        entry("user_task_1"),
    ]
    apply_labels(LabelFile(experiment="e", labels=entries), store)
    assert store.get_run(asked.run_id).ended_asking_user is True
    assert store.get_run(plain.run_id).ended_asking_user is None  # not annotated, not "no"


def test_committed_labels_annotate_every_unsolved_baseline_run() -> None:
    """D74: 6 runs of baseline_banking and 17 of baseline_banking_repeats, 16 asking the user."""
    first = load_labels(LABELS / "baseline_banking.yaml").labels
    repeats = load_labels(LABELS / "baseline_banking_repeats.yaml").labels
    everything = first + repeats
    assert (len(first), len(repeats)) == (6, 17)
    assert all(e.ended_asking_user is not None for e in everything)
    assert sum(bool(e.ended_asking_user) for e in everything) == 16
    keys = {(e.model_id, e.user_task_id, e.repeat_idx) for e in everything}
    assert len(keys) == len(everything)  # no run labelled twice
    # D74: the two runs whose final message ignores a write they made.
    other = {(e.model_id, e.user_task_id) for e in everything if e.label == FailureLabel.OTHER}
    assert (M20, "user_task_10") in other and (M20, "user_task_9") in other


def t2_spec(task: str) -> RunSpec:
    return RunSpec(
        experiment="e", suite="banking", benchmark_version="v1.2.2", user_task_id=task,
        topology="t2", provider="fake", model_id="scripted", temperature=0.0,
        reasoning_effort=None, max_tokens=None, seed=1, repeat_idx=0,
    )


def test_a_worker_that_hits_its_round_limit_labels_the_run_worker_max_iterations(
    tmp_path: Path,
) -> None:
    """Set when the run is stored, from the trace: a `result` carrying the round-limit error.
    A solved run gets no label, even if a worker hit its limit along the way."""
    suite = load_banking_suite()
    stuck = [call("get_balance", {}, call_id=f"b{i}") for i in range(WORKER_MAX_ITERS + 1)]

    def script(task: str) -> list[Step]:
        then = script_for("ground-truth", suite, suite.get_user_task_by_id(task), None, "t2")
        if task == "user_task_8":  # passes whatever happens (D37): the planner "recovers"
            return [call("delegate_to_payments", {"instruction": "Look."}, call_id="d"),
                    *stuck, *then]
        return [call("delegate_to_payments", {"instruction": "Look."}, call_id="d"),
                *stuck, answer("I couldn't do it.")]

    def run_fn(spec: RunSpec, ctx: RunContext) -> CaseResult:
        llm = LabLLM(FakeModel(script(spec.user_task_id)), provider="fake", model_id="scripted")
        return run_case(suite, build_t2(llm), suite.get_user_task_by_id(spec.user_task_id), ctx)

    store = RunStore(tmp_path / "runs.sqlite")
    specs = [t2_spec("user_task_1"), t2_spec("user_task_8")]
    provenance = Provenance(config_hash="c", git_commit="g", agentdojo_version="v")
    Scheduler(store, run_fn, FakeClock(), provenance).run(specs)
    failed, solved = (store.get_run(s.run_id) for s in specs)
    assert (failed.utility, failed.failure_label) == (False, "worker_max_iterations")
    assert (solved.utility, solved.failure_label) == (True, None)
    assert FailureLabel(failed.failure_label) is FailureLabel.WORKER_MAX_ITERATIONS
    # A run where no worker got stuck, solved or not, is left for the hand labels.
    assert automatic_label(False, []) is None
