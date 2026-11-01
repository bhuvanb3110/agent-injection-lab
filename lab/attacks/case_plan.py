"""What the runner does with one attack case (PLAN_M3 step 8): a case -> what `run_case` takes.

- The payloads: AgentDojo's own `important_instructions` cases are placed as in milestone 2
  (`build_injections`); every other technique by `place` (lab.attacks.placement), which also gives
  the tool-description injections and the markers.
- The success check, by the case's `check` name (D93):
  - `agentdojo:<injection task>.security`: the injection task's own check, scored by `run_case`
    exactly as in milestone 2;
  - a lab predicate (D96): a state check on the starting and final environments and one canary,
    wrapped as `run_case`'s `attack_check`. The canary is the last vector's: for multi_hop that is
    stage 2, the action (placement.py); every other case has one vector.
- The fields recorded with the run (`CaseFields`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from agentdojo.base_tasks import BaseInjectionTask
from agentdojo.functions_runtime import FunctionCall
from agentdojo.task_suite.task_suite import TaskSuite

from lab.attacks.agentdojo import ATTACK_NAME, build_injections, markers_for
from lab.attacks.cases import AttackCase
from lab.attacks.composition import Split
from lab.attacks.placement import place
from lab.attacks.predicates import lab_predicate
from lab.domains.agentdojo_bridge.run_case import AttackCheck
from lab.models.types import CaseFields, ToolDescriptionInjection
from lab.tracing.markers import Marker

AGENTDOJO_CHECK_PREFIX = "agentdojo:"


@dataclass(frozen=True)
class CasePlan:
    """Pass `injections`, `tool_descriptions` and `result_declarations` to `run_case`, with the
    `injection_task` or the `attack_check` (never both: one of them is None)."""

    case: AttackCase
    injections: dict[str, str]
    tool_descriptions: dict[str, ToolDescriptionInjection]
    result_declarations: dict[str, tuple[str, ...]]
    markers: list[Marker]
    injection_task: BaseInjectionTask[Any] | None
    attack_check: AttackCheck[Any] | None
    fields: CaseFields = field(compare=False)

    @property
    def canary(self) -> str:
        """The canary of the last vector (the module docstring says why the last)."""
        return [m.value for m in self.markers if m.kind == "canary"][-1]


def lab_attack_check(check: str, canary: str) -> AttackCheck[Any]:
    """A lab predicate (pre, post, canary) as `run_case`'s check. It never sees the text."""
    predicate = lab_predicate(check)

    def attack_check(
        output_text: str, trace: list[FunctionCall], pre: Any, post: Any
    ) -> bool:
        return predicate(pre, post, canary)

    return attack_check


def plan_case(suite: TaskSuite[Any], case: AttackCase, split: Split) -> CasePlan:
    """`split` picks the wording of the payload (dev or test) and is the case's own split."""
    fields = CaseFields(case.technique, case.channel, case.goal, case.t2_stratum)
    injection_task = suite.get_injection_task_by_id(case.injection_task_id) if (
        case.check.startswith(AGENTDOJO_CHECK_PREFIX)
    ) else None

    if case.technique == "important_instructions":
        user_task = suite.get_user_task_by_id(case.user_task_id)
        assert injection_task is not None  # the case schema ties the check to the task
        injections = build_injections(suite, ATTACK_NAME, user_task, injection_task)
        markers = markers_for(ATTACK_NAME, case.injection_task_id, case.user_task_id, injections)
        # Same vectors as the case lists: the dataset is built from get_injection_candidates.
        if sorted(injections) != sorted(case.vectors):
            raise ValueError(f"{case.id}: the payload vectors differ from the case's vectors")
        return CasePlan(case, injections, {}, {}, markers, injection_task, None, fields)

    placement = place(case, split)
    plan = CasePlan(
        case, placement.injections, placement.tool_descriptions, placement.result_declarations,
        placement.markers, injection_task, None, fields,
    )
    if injection_task is not None:
        return plan
    return replace(plan, attack_check=lab_attack_check(case.check, plan.canary))
