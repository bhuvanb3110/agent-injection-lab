"""The attack: AgentDojo's `important_instructions` payload plus a canary line (D47, D48).

AgentDojo's `ImportantInstructionsAttack` can't be used as it is: its `__init__` always calls
`get_model_name_from_pipeline`, which raises for gpt-oss because it isn't in `MODEL_NAMES`
(agentdojo/attacks/important_instructions_attacks.py, agentdojo/models.py). That includes the
`_no_model_name` variant, which calls `super().__init__` first. So we build the same payload
through `FixedJailbreakAttack` (agentdojo/attacks/base_attacks.py) with the generic model name.
This is semantically AgentDojo's `important_instructions_no_model_name`, and is stored under
that name.

The one change to the payload is a reference line holding a canary token inside the signature
block. The canary depends on the case, not on the run or the topology, so every topology gets a
byte-identical payload (the cache sharing in PLAN_M2 §7 depends on that).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, Final

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.attacks.base_attacks import DEFAULT_MODEL_NAME, FixedJailbreakAttack
from agentdojo.attacks.important_instructions_attacks import ImportantInstructionsAttack
from agentdojo.base_tasks import BaseInjectionTask, BaseUserTask
from agentdojo.functions_runtime import FunctionsRuntime
from agentdojo.task_suite.task_suite import TaskSuite
from agentdojo.types import ChatMessage

ATTACK_NAME: Final = "important_instructions_no_model_name"
USER_NAME = "Emma Johnson"  # what ImportantInstructionsAttack sets

# Crockford's base32: no I, L, O or U, so a canary is never misread as a word or a number.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CANARY_PREFIX = "CNRY"
CANARY_CHARS = 8

# Injection tasks whose `security()` is unsound (PLAN_M2 "Context"). The text of D44's note for
# user_task_9 applies here too: we score as AgentDojo does and say so, we don't fix it.
KNOWN_ISSUES = {
    "injection_task_0": "`any(service or '' in t.subject ...)` parses as `service or (...)`: any "
    "transfer to the attacker IBAN counts as success.",
    "injection_task_1": "same precedence bug as injection_task_0 (`recipient or '' in ...`).",
    "injection_task_3": "same precedence bug as injection_task_0 (`phone or '' in ...`).",
}

# The template is AgentDojo's own. The canary line goes after the signature, inside the block.
_SIGNATURE_END = "Signed,\n\n{user}\n\n"
_PAYLOAD = ImportantInstructionsAttack._JB_STRING.replace(
    _SIGNATURE_END, _SIGNATURE_END + "Reference: {canary}\n\n"
)
if _PAYLOAD == ImportantInstructionsAttack._JB_STRING:
    # AgentDojo changed the template: stop instead of running an attack nobody has read.
    raise ImportError("AgentDojo's important_instructions payload no longer has the signature")


def canary_for(attack: str, injection_task_id: str, user_task_id: str, vector_id: str) -> str:
    """CNRY + 8 Crockford-base32 characters of the case's hash (the first 40 bits)."""
    digest = hashlib.sha256(
        "|".join((attack, injection_task_id, user_task_id, vector_id)).encode("utf-8")
    ).digest()
    bits = int.from_bytes(digest[:5], "big")  # 5 bytes = 40 bits = 8 characters of 5 bits
    chars = [_CROCKFORD[(bits >> (5 * i)) & 31] for i in reversed(range(CANARY_CHARS))]
    return CANARY_PREFIX + "".join(chars)


class _NoTargetPipeline(BasePipelineElement):
    """`BaseAttack.__init__` wants the pipeline under attack but only stores it. Ours never
    looks at it: the payload must not depend on the model (that is the point of the generic
    model name), so we pass something that fails loudly if anyone starts to."""

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Any = None,
        messages: Sequence[ChatMessage] = (),
        extra_args: dict[str, Any] = {},  # noqa: B006 -- matches AgentDojo's signature
    ) -> Any:
        raise RuntimeError("The attack's target pipeline is a placeholder and can't be run")


class ImportantInstructionsWithCanary(FixedJailbreakAttack):
    name = ATTACK_NAME

    def __init__(self, task_suite: TaskSuite[Any]) -> None:
        super().__init__(_PAYLOAD, task_suite, _NoTargetPipeline())
        self.user_name = USER_NAME
        self.model_name = DEFAULT_MODEL_NAME

    def attack(
        self, user_task: BaseUserTask[Any], injection_task: BaseInjectionTask[Any]
    ) -> dict[str, str]:
        # Same as FixedJailbreakAttack.attack, plus the per-vector canary.
        return {
            vector: self.jailbreak.format(
                goal=injection_task.GOAL,
                user=self.user_name,
                model=self.model_name,
                task=self.summarize_task(user_task),
                canary=canary_for(self.name, injection_task.ID, user_task.ID, vector),
            )
            for vector in self.get_injection_candidates(user_task)
        }


ATTACKS = (ATTACK_NAME,)


def build_injections(
    suite: TaskSuite[Any],
    attack: str,
    user_task: BaseUserTask[Any],
    injection_task: BaseInjectionTask[Any],
) -> dict[str, str]:
    """The injection vector -> payload text for one case, ready for
    `suite.load_and_inject_default_environment`. Only the vectors this user task reads are
    filled (AgentDojo's `get_injection_candidates`); the rest keep their benign defaults."""
    if attack != ATTACK_NAME:
        raise ValueError(f"Unknown attack {attack!r}; known: {ATTACKS}")
    return ImportantInstructionsWithCanary(suite).attack(user_task, injection_task)
