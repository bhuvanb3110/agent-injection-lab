"""Experiment configs (configs/experiments/*.yaml) and their expansion into RunSpecs (D32).

A config names the tasks, the topology, the models and the number of repeats. From milestone 2
it can also name an attack, which adds one attacked run per (user task, injection task) and,
unless `include_benign` is false, the benign control of each user task. Sampling
(temperature, reasoning effort) is not in the config: it is fixed per model in
configs/providers.yaml (D19), so two experiments can't run the same model differently by
accident. Every value that can change a result ends up in each RunSpec, so in its run ID.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lab.attacks.agentdojo import ATTACK_NAME
from lab.domains.agentdojo_bridge import (
    BANKING_SUITE_NAME,
    BENCHMARK_VERSION,
    banking_injection_task_ids,
    banking_task_ids,
)
from lab.models.fake import FAKE_PROVIDER, fake_model_info
from lab.models.providers import ModelInfo, ProviderConfig
from lab.scheduler.spec import RunSpec


class _Strict(BaseModel):
    # A typo in the YAML must fail loudly, not silently fall back to a default.
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelRef(_Strict):
    provider: str
    model_id: str


class AttackConfig(_Strict):
    name: Literal["important_instructions_no_model_name"] = ATTACK_NAME
    injection_tasks: list[str] | Literal["all"]

    def task_ids(self) -> list[str]:
        known = banking_injection_task_ids()
        if self.injection_tasks == "all":
            return known
        unknown = [t for t in self.injection_tasks if t not in known]
        if unknown:
            raise ValueError(f"Unknown banking injection tasks: {unknown}")
        return list(self.injection_tasks)


# Config fields added after milestone 1, with the value that means "not used". At that value a
# field is left out of the config hash, so every milestone-1 config keeps its hash (D48).
_ADDED_FIELDS = {"topologies": None, "attack": None, "include_benign": True}


class ExperimentConfig(_Strict):
    name: str
    description: str = ""
    suite: Literal["banking"]  # milestones 1-4 are Banking only
    # Give either `topology` or `topologies`. T2-T5 are added to the Literal as they are built.
    topology: Literal["t1", "t2"] | None = None
    topologies: list[Literal["t1", "t2"]] | None = None
    user_tasks: list[str] | Literal["all"]
    models: list[ModelRef] = Field(min_length=1)
    # A count n means repeat indices 0..n-1. A list names the indices, so a later experiment can
    # add repeats to an earlier one without re-running the repeats it already has (D45).
    repeats: int | list[int]
    seed: int  # base seed; a run's seed is seed + repeat_idx (D27)
    max_tokens: int = Field(ge=1)  # sent on every call (D28)
    attack: AttackConfig | None = None
    include_benign: bool = True  # with an attack: also run each user task without injections

    @model_validator(mode="after")
    def _one_way_to_name_the_topology(self) -> ExperimentConfig:
        if (self.topology is None) == (self.topologies is None):
            raise ValueError("give exactly one of `topology` and `topologies`")
        if self.topologies is not None and (
            not self.topologies or len(set(self.topologies)) != len(self.topologies)
        ):
            raise ValueError("`topologies` must be a non-empty list of distinct topologies")
        if self.attack is None and not self.include_benign:
            raise ValueError("include_benign: false with no attack leaves nothing to run")
        return self

    def topology_names(self) -> list[str]:
        if self.topologies is not None:
            return list(self.topologies)
        assert self.topology is not None  # the validator guarantees one of the two
        return [self.topology]

    @field_validator("repeats")
    @classmethod
    def _check_repeats(cls, value: int | list[int]) -> int | list[int]:
        if isinstance(value, int):
            if value < 1:
                raise ValueError("repeats must be at least 1")
        elif not value or len(set(value)) != len(value) or min(value) < 0:
            raise ValueError("repeat indices must be a non-empty list of distinct indices >= 0")
        return value

    def repeat_indices(self) -> list[int]:
        return list(range(self.repeats)) if isinstance(self.repeats, int) else list(self.repeats)

    def task_ids(self) -> list[str]:
        known = banking_task_ids()
        if self.user_tasks == "all":
            return known
        unknown = [t for t in self.user_tasks if t not in known]
        if unknown:
            raise ValueError(f"Unknown {self.suite} user tasks: {unknown}")
        return list(self.user_tasks)

    @property
    def config_hash(self) -> str:
        fields = self.model_dump(mode="json")
        for name, default in _ADDED_FIELDS.items():
            if fields[name] == default:
                del fields[name]
        if fields["topology"] is None:
            del fields["topology"]
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_experiment(path: Path) -> ExperimentConfig:
    return ExperimentConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def model_info(ref: ModelRef, providers: dict[str, ProviderConfig]) -> ModelInfo:
    """Look a model up in configs/providers.yaml, or return the FakeModel's fixed entry."""
    if ref.provider == FAKE_PROVIDER:
        return fake_model_info()
    provider = providers.get(ref.provider)
    if provider is None or ref.model_id not in provider.models:
        raise ValueError(f"{ref.provider}/{ref.model_id} is not in configs/providers.yaml")
    info = provider.models[ref.model_id]
    if not info.tool_calling:
        raise ValueError(f"{ref.provider}/{ref.model_id} has no tool calling; it can't be an agent")
    return info


def expand(config: ExperimentConfig, providers: dict[str, ProviderConfig]) -> list[RunSpec]:
    """One RunSpec per (repeat, case, model, topology).

    A case is a user task run benign, or run with one injection task. Within a user task the
    benign control comes first, then its attacked cases in injection task order.

    Models are interleaved, so when one model hits its daily cap, every model has made the same
    progress through the case list and a partial day still gives a paired comparison. The
    topologies of one case and model run together for the same reason.
    """
    infos = [(ref, model_info(ref, providers)) for ref in config.models]
    cases: list[tuple[str, str | None]] = []  # (user task, injection task or None)
    for task_id in config.task_ids():
        if config.attack is None or config.include_benign:
            cases.append((task_id, None))
        if config.attack is not None:
            cases += [(task_id, inj) for inj in config.attack.task_ids()]
    attack_name = config.attack.name if config.attack is not None else None
    specs = []
    for repeat_idx in config.repeat_indices():
        for task_id, injection_task_id in cases:
            for ref, info in infos:
                if info.sampling is None:
                    raise ValueError(f"{ref.provider}/{ref.model_id} has no sampling settings")
                for topology in config.topology_names():
                    specs.append(
                        RunSpec(
                            experiment=config.name,
                            suite=BANKING_SUITE_NAME,
                            benchmark_version=BENCHMARK_VERSION,
                            user_task_id=task_id,
                            topology=topology,
                            provider=ref.provider,
                            model_id=ref.model_id,
                            temperature=info.sampling.temperature,
                            reasoning_effort=info.sampling.reasoning_effort,
                            max_tokens=config.max_tokens,
                            seed=config.seed + repeat_idx,
                            repeat_idx=repeat_idx,
                            attack=attack_name if injection_task_id is not None else None,
                            injection_task_id=injection_task_id,
                        )
                    )
    return specs
