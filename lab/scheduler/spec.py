"""RunSpec: one unit of work in the queue, and the source of its run ID (DECISIONS.md D24)."""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, model_validator

from lab.models.providers import ReasoningEffort

_SHA256 = re.compile(r"[0-9a-f]{64}")


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
    # Added in milestone 3: the attack case (lab.attacks.cases) a run was built from. Set only for
    # runs of a config's `cases:`, where `attack` is then the case's technique. None is left out
    # of the run ID, so every earlier run keeps its ID (D48 pattern). Different cases of one user
    # task and injection task have different IDs, so two cases never share a run ID.
    case_id: str | None = None
    # Added in milestone 3 (step 10): the SHA-256 of PREREGISTRATION.md for a run made under the
    # preregistration (`lab run` sets it on registered runs, D102). Recorded only: it is never
    # part of the run ID, whatever its value. So an amendment, which changes the prereg's hash,
    # changes no run ID, and a resumed sweep carries on. Which committed version of the prereg
    # a run belongs to is checked from the store by `validate-rules --store` (D117).
    prereg_sha256: str | None = None

    @model_validator(mode="after")
    def _attack_and_injection_task_go_together(self) -> RunSpec:
        if (self.attack is None) != (self.injection_task_id is None):
            raise ValueError("attack and injection_task_id must both be set, or both be None")
        if self.case_id is not None and self.attack is None:
            raise ValueError("a case_id needs the case's attack and injection_task_id")
        if self.prereg_sha256 is not None and not _SHA256.fullmatch(self.prereg_sha256):
            raise ValueError("prereg_sha256 must be 64 lowercase hex characters")
        return self

    @property
    def run_id(self) -> str:
        fields = self.model_dump(mode="json")
        for name in ("attack", "injection_task_id", "case_id"):
            if fields[name] is None:
                del fields[name]
        del fields["prereg_sha256"]  # recorded only, never part of the ID
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
