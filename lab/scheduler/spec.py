"""RunSpec: one unit of work in the queue, and the source of its run ID (DECISIONS.md D24)."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict

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

    @property
    def run_id(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
