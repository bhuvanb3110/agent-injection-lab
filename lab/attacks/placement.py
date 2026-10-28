"""Where a case's payloads go (PLAN_M3 §3.1, §3.5, D94): case + wording -> a run's injections.

An environment vector (`injection_*`) is filled in the starting environment, as AgentDojo does,
and the agent meets the text in a tool result. A `tool_description:<tool>` vector is appended to
that tool's description, and every agent holding the tool is sent it with its tools; the trace
records the delivery as a `tool_schema` event, the hop-0 source of that text.

Multi-hop has two vectors, in this order (PLAN_M3 §3.1):
- stage 1, a pointer, in `injection_incoming_transaction`: it carries no goal, only the canary
  and the name of the file to read;
- stage 2, the action, in `injection_address_change` (`address-change.txt`): the goal.
In T2 the first is read by the payments worker and the second by the account worker, so the
pointer must go through the planner (the case's `required_hops`).

Every vector has its own canary, so the trace can tell the stages apart.

An obfuscated payload hides its goal markers from the marker search (base64, Unicode tags). Where
the payload is delivered, the trace says which markers it carries: a tool description in the
`tool_schema` event, a payload in the environment in the event of the tool result that holds its
canary (`Placement.result_declarations`, D111). Both are hop 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from lab.attacks.agentdojo import GOAL_MARKERS, canary_for
from lab.attacks.cases import TOOL_DESCRIPTION_PREFIX, AttackCase
from lab.attacks.payload import Payload, Stage, build_payload
from lab.attacks.techniques import Split
from lab.domains.agentdojo_bridge import load_banking_suite
from lab.models.types import ToolDescriptionInjection
from lab.tracing.markers import Marker

MULTI_HOP_VECTORS: Final = ("injection_incoming_transaction", "injection_address_change")
MULTI_HOP_STAGES: Final[tuple[Stage, Stage]] = ("pointer", "action")


@dataclass(frozen=True)
class Placement:
    """What a case puts into a run."""

    # Environment vector -> text, for `load_and_inject_default_environment`.
    injections: dict[str, str] = field(default_factory=dict)
    # Tool name -> payload appended to its description.
    tool_descriptions: dict[str, ToolDescriptionInjection] = field(default_factory=dict)
    # Everything the trace is searched for: the goal markers, then one canary per vector.
    markers: list[Marker] = field(default_factory=list)
    # Canary -> the goal markers an obfuscated payload in the environment carries only in encoded
    # form. Pass it to `run_case` with the injections (D111). Empty unless technique=obfuscation.
    result_declarations: dict[str, tuple[str, ...]] = field(default_factory=dict)


def stages_for(case: AttackCase) -> list[Stage | None]:
    """The multi_hop stage of each vector, in vector order (None for any other technique)."""
    if case.technique != "multi_hop":
        return [None] * len(case.vectors)
    if tuple(case.vectors) != MULTI_HOP_VECTORS:
        raise ValueError(f"{case.id}: a multi_hop case needs the vectors {MULTI_HOP_VECTORS}")
    return list(MULTI_HOP_STAGES)


def place(
    case: AttackCase,
    split: Split,
    *,
    goal_text: str | None = None,
    goal_markers: tuple[str, ...] | None = None,
) -> Placement:
    """The case's payloads, each in its vector. `goal_text` and `goal_markers` default to the
    injection task's own (its GOAL and `GOAL_MARKERS`)."""
    if case.technique == "important_instructions":
        raise ValueError("important_instructions is placed by lab.attacks.agentdojo")
    if goal_text is None:
        goal_text = load_banking_suite().get_injection_task_by_id(case.injection_task_id).GOAL
    if goal_markers is None:
        goal_markers = GOAL_MARKERS[case.injection_task_id]

    injections: dict[str, str] = {}
    tool_descriptions: dict[str, ToolDescriptionInjection] = {}
    result_declarations: dict[str, tuple[str, ...]] = {}
    canaries: list[Marker] = []
    for vector, stage in zip(case.vectors, stages_for(case), strict=True):
        canary = canary_for(case.technique, case.injection_task_id, case.user_task_id, vector)
        payload = build_payload(
            case.technique, split, goal=case.goal, goal_text=goal_text,
            goal_markers=goal_markers, canary=canary, stage=stage,
        )
        if vector.startswith(TOOL_DESCRIPTION_PREFIX):
            tool = vector.removeprefix(TOOL_DESCRIPTION_PREFIX)
            tool_descriptions[tool] = _description_injection(payload)
        else:
            injections[vector] = payload.text
            if payload.decoded_markers:
                # The encoded goal is declared on the tool result that delivers it (D111).
                result_declarations[canary] = tuple(m.value for m in payload.decoded_markers)
        canaries.append(Marker(value=canary, kind="canary"))
    goals = [Marker(value=m, kind="goal") for m in goal_markers]
    return Placement(injections, tool_descriptions, [*goals, *canaries], result_declarations)


def _description_injection(payload: Payload) -> ToolDescriptionInjection:
    return ToolDescriptionInjection(
        text=payload.text, declared=tuple(m.value for m in payload.decoded_markers)
    )
