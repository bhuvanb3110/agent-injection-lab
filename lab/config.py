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
from lab.attacks.case_set import CaseSet, committed_case_set
from lab.attacks.cases import AttackCase
from lab.domains.agentdojo_bridge import (
    BANKING_SUITE_NAME,
    BENCHMARK_VERSION,
    banking_injection_task_ids,
    banking_task_ids,
)
from lab.models.fake import FAKE_PROVIDER, fake_model_info
from lab.models.providers import ModelInfo, ProviderConfig
from lab.scheduler.spec import RunSpec
from lab.topologies.presets import TopologyName


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
_ADDED_FIELDS = {"topologies": None, "attack": None, "include_benign": True, "cases": None}


class ExperimentConfig(_Strict):
    name: str
    description: str = ""
    suite: Literal["banking"]  # milestones 1-4 are Banking only
    # Give either `topology` or `topologies`: names from the presets table (PLAN_M2 Q3), which
    # grows as topologies are built.
    topology: TopologyName | None = None
    topologies: list[TopologyName] | None = None
    user_tasks: list[str] | Literal["all"]
    models: list[ModelRef] = Field(min_length=1)
    # A count n means repeat indices 0..n-1. A list names the indices, so a later experiment can
    # add repeats to an earlier one without re-running the repeats it already has (D45).
    repeats: int | list[int]
    seed: int  # base seed; a run's seed is seed + repeat_idx (D27)
    max_tokens: int = Field(ge=1)  # sent on every call (D28)
    attack: AttackConfig | None = None
    include_benign: bool = True  # with an attack: also run each user task without injections
    # Added in milestone 3: the attack cases to run (PLAN_M3 step 2), as the name of a set or a
    # list of case IDs (the step-8 dev configs). None keeps the milestone-2 behaviour, where
    # `attack` and the task lists decide the cases. Only a list of dev case IDs can be expanded
    # so far (see `expand`); the task lists are then unused.
    cases: str | list[str] | None = None

    @model_validator(mode="after")
    def _one_way_to_name_the_topology(self) -> ExperimentConfig:
        if (self.topology is None) == (self.topologies is None):
            raise ValueError("give exactly one of `topology` and `topologies`")
        if self.topologies is not None and (
            not self.topologies or len(set(self.topologies)) != len(self.topologies)
        ):
            raise ValueError("`topologies` must be a non-empty list of distinct topologies")
        if self.cases is not None and not self.cases:
            raise ValueError("`cases` must name a set or list at least one case")
        if self.attack is None and self.cases is None and not self.include_benign:
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


def listed_cases(config: ExperimentConfig, case_set: CaseSet) -> list[AttackCase]:
    """The attack cases a `cases:` list names, in list order.

    Which split a case is in is not decided here. A list with test-split cases is expanded like
    any other; `lab run` refuses it unless `validate-rules` group A passes (lab.rules.guard,
    D102, D116). Unknown and repeated IDs are errors: a repeated one would make two identical
    run IDs, and the second would be silently skipped.
    """
    ids = config.cases
    if not isinstance(ids, list):
        raise ValueError(
            f"cases: {ids!r}: naming a set of cases is not built yet; give a list of case IDs"
        )
    if config.attack is not None:
        raise ValueError("give `cases` or `attack`, not both")
    if config.include_benign:
        # A case list runs attack cases only. Say so, rather than let the default hide that no
        # benign control is run.
        raise ValueError("`cases` runs attack cases only; set `include_benign: false`")
    unknown = [i for i in ids if i not in case_set.cases]
    if unknown:
        raise ValueError(f"Unknown attack cases: {unknown}")
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise ValueError(f"Attack cases listed more than once: {repeated}")
    return [case_set.cases[i] for i in ids]


def expand(
    config: ExperimentConfig,
    providers: dict[str, ProviderConfig],
    case_set: CaseSet | None = None,
    prereg_sha256: str | None = None,
) -> list[RunSpec]:
    """One RunSpec per (repeat, case, model, topology).

    A case is a user task run benign, or run with one injection task. Within a user task the
    benign control comes first, then its attacked cases in injection task order. With a `cases:`
    list a case is an attack case of the dataset (`case_set`, by default the committed one), and
    the order is the list's.

    `prereg_sha256` is stamped on every spec (None for a run made outside the preregistration).

    Models are interleaved, so when one model hits its daily cap, every model has made the same
    progress through the case list and a partial day still gives a paired comparison. The
    topologies of one case and model run together for the same reason.
    """
    infos = [(ref, model_info(ref, providers)) for ref in config.models]
    # (user task, injection task or None, the attack case if the config lists cases)
    cases: list[tuple[str, str | None, AttackCase | None]] = []
    if config.cases is not None:
        listed = listed_cases(config, case_set or committed_case_set(config.suite))
        cases = [(c.user_task_id, c.injection_task_id, c) for c in listed]
    else:
        for task_id in config.task_ids():
            if config.attack is None or config.include_benign:
                cases.append((task_id, None, None))
            if config.attack is not None:
                cases += [(task_id, inj, None) for inj in config.attack.task_ids()]
    attack_name = config.attack.name if config.attack is not None else None
    specs = []
    for repeat_idx in config.repeat_indices():
        for task_id, injection_task_id, attack_case in cases:
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
                            # For a case, the attack is its technique (M3 step 8).
                            attack=(
                                attack_case.technique if attack_case is not None
                                else attack_name if injection_task_id is not None else None
                            ),
                            injection_task_id=injection_task_id,
                            case_id=attack_case.id if attack_case is not None else None,
                            prereg_sha256=prereg_sha256,
                        )
                    )
    return specs
