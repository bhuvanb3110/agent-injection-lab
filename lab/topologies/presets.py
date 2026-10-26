"""Topology presets: a base shape plus a fixed set of oversight components (PLAN_M2 Q3).

Each oversight mechanism is written once (lab/defenses/) and attached at a named checkpoint.
A component is `<mechanism>@<checkpoint>`. A preset is a base and its components, and nothing
is ever stacked on itself: a component appears at most once.

| Preset | Base            | Components                                 | Parent |
| ------ | --------------- | ------------------------------------------ | ------ |
| t1     | single          | none                                       |        |
| t2     | planner_workers | none                                       |        |
| t3     | planner_workers | llm_reviewer@handoff, llm_reviewer@action  | t2     |
| t4     | planner_workers | policy_v1@action                           | t2     |
| t5     | planner_workers | t3's + human_raw@action                    | t3     |
| t5_summary | planner_workers | t3's + human_summary@action            | t3     |

A child's runs match its parent's call for call until its oversight first intervenes, which
is what `prefix_divergence` measures (the scheduler records it, D66). At the action checkpoint
the reviewer decides first and the human second, so the human is asked only about calls the
reviewer approved. M4's defense sweep reuses the same components on the bases: T2 +
`policy_v1@action` *is* T4.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal, get_args

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement

from lab.defenses.gates import CHECKPOINTS, Checkpoint, Gate
from lab.defenses.human import RAW as HUMAN_RAW
from lab.defenses.human import SUMMARY as HUMAN_SUMMARY
from lab.defenses.human import RawHuman, SummaryHuman
from lab.defenses.llm_reviewer import NAME as LLM_REVIEWER
from lab.defenses.llm_reviewer import LLMReviewer
from lab.defenses.policy import PolicyGate, load_policy
from lab.domains.agentdojo_bridge.banking import load_banking_suite
from lab.models.llm_element import LabLLM
from lab.topologies.t1 import build_t1
from lab.topologies.t2 import build_t2

TopologyName = Literal["t1", "t2", "t3", "t4", "t5", "t5_summary"]
Base = Literal["single", "planner_workers"]
POLICY_V1 = "policy_v1"


@dataclass(frozen=True)
class Preset:
    base: Base
    components: tuple[str, ...] = ()
    parent: TopologyName | None = None  # the preset it equals until its oversight acts


_T3 = (f"{LLM_REVIEWER}@handoff", f"{LLM_REVIEWER}@action")
PRESETS: dict[TopologyName, Preset] = {
    "t1": Preset("single"),
    "t2": Preset("planner_workers"),
    "t3": Preset("planner_workers", _T3, parent="t2"),
    "t4": Preset("planner_workers", (f"{POLICY_V1}@action",), parent="t2"),
    "t5": Preset("planner_workers", (*_T3, f"{HUMAN_RAW}@action"), parent="t3"),
    "t5_summary": Preset("planner_workers", (*_T3, f"{HUMAN_SUMMARY}@action"), parent="t3"),
}
assert set(PRESETS) == set(get_args(TopologyName))


def parents() -> dict[str, str]:
    """topology -> parent topology, for the presets that have one (prefix_divergence)."""
    return {name: p.parent for name, p in PRESETS.items() if p.parent is not None}


def parse_components(components: tuple[str, ...]) -> dict[str, frozenset[Checkpoint]]:
    """mechanism -> its checkpoints. Raises on a malformed, unknown or repeated component."""
    if len(set(components)) != len(components):
        raise ValueError(f"A component is stacked on itself: {components}")
    by_mechanism: dict[str, set[Checkpoint]] = defaultdict(set)
    for component in components:
        mechanism, _, checkpoint = component.partition("@")
        if checkpoint not in CHECKPOINTS:
            raise ValueError(f"Unknown checkpoint in component {component!r}")
        by_mechanism[mechanism].add(checkpoint)
    return {m: frozenset(c) for m, c in by_mechanism.items()}


def build_gates(components: tuple[str, ...], llm: LabLLM) -> list[Gate]:
    """One gate per mechanism, attached at each of its checkpoints, in the table's order."""
    gates: list[Gate] = []
    for mechanism, checkpoints in parse_components(components).items():
        if mechanism == LLM_REVIEWER:
            gates.append(LLMReviewer(llm, checkpoints))  # on the run's model (PLAN_M2 Q2)
        elif mechanism == POLICY_V1:
            gates.append(PolicyGate(load_policy(), checkpoints))
        elif mechanism == HUMAN_RAW:
            gates.append(RawHuman(load_banking_suite(), checkpoints))
        elif mechanism == HUMAN_SUMMARY:
            # The proposing worker writes the summary, on the run's model (PLAN_M2 Q4).
            gates.append(SummaryHuman(load_banking_suite(), llm, checkpoints))
        else:
            raise ValueError(f"Unknown oversight mechanism {mechanism!r}")
    return gates


def build(topology: str, llm: LabLLM) -> BasePipelineElement:
    """The pipeline of a preset, around the run's model."""
    if topology not in PRESETS:
        raise ValueError(f"Unknown topology {topology!r}; known: {sorted(PRESETS)}")
    preset = PRESETS[topology]
    if preset.base == "single":
        if preset.components:
            raise ValueError("Gates on the single agent come with M4's defense sweep")
        return build_t1(llm)
    return build_t2(llm, build_gates(preset.components, llm), name=topology)
