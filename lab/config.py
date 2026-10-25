"""Experiment configs (configs/experiments/*.yaml) and their expansion into RunSpecs (D32).

A config names the tasks, the topology, the models and the number of repeats. Sampling
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
from pydantic import BaseModel, ConfigDict, Field

from lab.domains.agentdojo_bridge import BANKING_SUITE_NAME, BENCHMARK_VERSION, banking_task_ids
from lab.models.fake import FAKE_PROVIDER, fake_model_info
from lab.models.providers import ModelInfo, ProviderConfig
from lab.scheduler.spec import RunSpec


class _Strict(BaseModel):
    # A typo in the YAML must fail loudly, not silently fall back to a default.
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelRef(_Strict):
    provider: str
    model_id: str


class ExperimentConfig(_Strict):
    name: str
    description: str = ""
    suite: Literal["banking"]  # milestones 1-4 are Banking only
    topology: Literal["t1"]  # T2-T5 come in milestone 2
    user_tasks: list[str] | Literal["all"]
    models: list[ModelRef] = Field(min_length=1)
    repeats: int = Field(ge=1)
    seed: int  # base seed; a run's seed is seed + repeat_idx (D27)
    max_tokens: int = Field(ge=1)  # sent on every call (D28)

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
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
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
    """One RunSpec per (repeat, task, model), models interleaved.

    Interleaving means that when one model hits its daily cap, every model has made the same
    progress through the task list, so a partial day still gives a paired comparison.
    """
    infos = [(ref, model_info(ref, providers)) for ref in config.models]
    specs = []
    for repeat_idx in range(config.repeats):
        for task_id in config.task_ids():
            for ref, info in infos:
                if info.sampling is None:
                    raise ValueError(f"{ref.provider}/{ref.model_id} has no sampling settings")
                specs.append(
                    RunSpec(
                        experiment=config.name,
                        suite=BANKING_SUITE_NAME,
                        benchmark_version=BENCHMARK_VERSION,
                        user_task_id=task_id,
                        topology=config.topology,
                        provider=ref.provider,
                        model_id=ref.model_id,
                        temperature=info.sampling.temperature,
                        reasoning_effort=info.sampling.reasoning_effort,
                        max_tokens=config.max_tokens,
                        seed=config.seed + repeat_idx,
                        repeat_idx=repeat_idx,
                    )
                )
    return specs
