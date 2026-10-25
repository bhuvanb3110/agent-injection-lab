"""Hand failure labels: the committed baseline file, and writing labels into the store (D43)."""

from pathlib import Path

import pytest

from lab.scheduler.spec import RunSpec
from lab.scoring.failure_labels import (
    FailureLabel,
    LabelEntry,
    LabelFile,
    apply_labels,
    load_labels,
)
from lab.store import RunRecord, RunStore

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
