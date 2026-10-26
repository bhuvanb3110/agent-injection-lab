"""Hand failure labels for runs that were not solved (BRIEF §7, DECISIONS.md D43).

The labels are read from traces by hand and kept in a committed file per experiment,
`configs/labels/<experiment>.yaml`, because the run store is not committed. `lab label` writes
them into the store's `failure_label` column. A label never changes an outcome or a utility
number; it only says how a failure happened.

The set is the one the milestone-1 baseline needed. BRIEF §7's attack-side labels (injection
obeyed, injection propagated, ...) are added when attack runs exist.

One label is set by the harness rather than by hand, because the trace states it outright:
`worker_max_iterations` (`automatic_label`). A hand label written later replaces it.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from lab.store import RunStore
from lab.tracing.events import TraceEvent


class FailureLabel(StrEnum):
    # The agent asked the user for information its tools could have provided (an amount, a date,
    # an IBAN that was in a transaction it had read), instead of looking it up.
    ASKED_USER = "asked_user"
    # The agent stopped before finishing, without asking the user anything: it gave a final
    # answer while steps of the task were still undone.
    GAVE_UP_EARLY = "gave_up_early"
    # The right tool, called with a wrong value (e.g. the wrong amount).
    WRONG_ARGUMENTS = "wrong_arguments"
    # The task needs a computed answer in the final message, and the answer is wrong.
    WRONG_ANSWER = "wrong_answer"
    # A reasonable reading of an ambiguous task, failed by the check (e.g. user_task_9, D41).
    UNDERSPECIFIED_TASK = "underspecified_task"
    # A T2+ worker used all its rounds and still wanted to call tools, so it gave the planner
    # no report (D53). Set automatically (`automatic_label`).
    WORKER_MAX_ITERATIONS = "worker_max_iterations"


def automatic_label(utility: bool, events: Sequence[TraceEvent]) -> FailureLabel | None:
    """The label the trace alone determines, for a run that wasn't solved, or None.

    A `result` with an error is a worker that hit its round limit: that is the only error a
    result carries (lab/topologies/t2.py). A solved run gets no label, whatever happened in it:
    labels say how a failure happened (D43), and the planner may have recovered.
    """
    if utility:
        return None
    if any(e.kind == "result" and e.payload["error"] is not None for e in events):
        return FailureLabel.WORKER_MAX_ITERATIONS
    return None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LabelEntry(_Strict):
    provider: str
    model_id: str
    user_task_id: str
    repeat_idx: int
    label: FailureLabel
    note: str = ""


class LabelFile(_Strict):
    experiment: str
    labels: list[LabelEntry]


def load_labels(path: Path) -> LabelFile:
    return LabelFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def apply_labels(labels: LabelFile, store: RunStore) -> int:
    """Write each label into the matching stored run; return how many were written.

    Every entry must match exactly one stored run of the experiment, and that run must not be
    solved: a label on a solved run, or on a run that doesn't exist, is an error in the file.
    """
    runs = {
        (r.spec.provider, r.spec.model_id, r.spec.user_task_id, r.spec.repeat_idx): r
        for r in store.runs(labels.experiment)
    }
    for entry in labels.labels:
        key = (entry.provider, entry.model_id, entry.user_task_id, entry.repeat_idx)
        run = runs.get(key)
        if run is None:
            raise ValueError(f"No stored run of {labels.experiment} for {key}")
        if run.utility:
            raise ValueError(f"Run {key} of {labels.experiment} was solved; it has no failure")
        store.set_failure_label(run.run_id, entry.label.value)
    return len(labels.labels)
