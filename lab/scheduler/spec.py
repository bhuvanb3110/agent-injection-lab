"""RunSpec: one unit of work in the queue, and the source of its run ID (DECISIONS.md D24)."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, model_validator

from lab.models.providers import ReasoningEffort


class RunSpec(BaseModel):
    """Everything that defines a run. Two runs with the same spec are the same run.

    The run ID is a hash of these fields, so a completed run is recognised and skipped on
    resume. Anything that can change the result must be a field here. Provenance (git commit,
    package versions, date) is recorded on the run but deliberately *not* part of the ID: a
    fresh commit must not make every finished run look new.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment: str
    suite: str
    benchmark_version: str
    user_task_id: str
    topology: str
    provider: str
    model_id: str
    temperature: float
    reasoning_effort: ReasoningEffort | None
    max_tokens: int | None
    seed: int | None  # base seed + repeat_idx (D27); None: no seed is sent
    repeat_idx: int
    # Added in milestone 2. Both None means a benign run, and then they are left out of the run
    # ID, so every milestone-1 run keeps its ID (D48).
    attack: str | None = None
    injection_task_id: str | None = None

    @model_validator(mode="after")
    def _attack_and_injection_task_go_together(self) -> RunSpec:
        if (self.attack is None) != (self.injection_task_id is None):
            raise ValueError("attack and injection_task_id must both be set, or both be None")
        return self

    @property
    def run_id(self) -> str:
        fields = self.model_dump(mode="json")
        for name in ("attack", "injection_task_id"):
            if fields[name] is None:
                del fields[name]
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
