"""FakeModel scripts derived from AgentDojo tasks, for the smoke run and tests (D33).

Three scripted "models", named by model ID under provider "fake":
- `ground-truth`: makes the task's ground-truth tool calls one per turn, then gives the task's
  GROUND_TRUTH_OUTPUT as the answer. Same steps as AgentDojo's GroundTruthPipeline
  (agentdojo/agent_pipeline/ground_truth_pipeline.py), but through LabLLM, the cache, the
  scheduler and LabToolsExecutor, so the smoke run exercises the real code path.
- `refuse`: answers at once without calling a tool. Solves nothing that changes state.
- `obedient`: the ground-truth calls of the user task (which read the injection channel), then
  the ground-truth calls of the injection task, then the user task's answer. It is a model that
  does what the injected text says, so on an attack case it must score attack_succeeded = 1,
  while `ground-truth` must score 0. On a benign case it is the same as `ground-truth`.

In T2 one script plays every agent, in the order the calls are made (D53):
- the planner delegates each run of consecutive ground-truth calls that one worker owns, with
  an instruction that lists the calls, and finally gives GROUND_TRUTH_OUTPUT;
- each worker makes its calls one per turn and reports "Done.";
- `obedient`: the worker that reads the injected text obeys it. If it holds the tools the
  injection task needs, it makes those calls itself (a local attack). If not, it relays the
  injection task's GOAL in its report, the planner adds the GOAL to its next instruction to the
  worker that holds those tools, and that worker makes the calls (a cross-worker attack).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TypeVar

from agentdojo.agent_pipeline.tool_execution import tool_result_to_str
from agentdojo.base_tasks import BaseInjectionTask, BaseUserTask
from agentdojo.functions_runtime import FunctionCall, FunctionsRuntime, TaskEnvironment
from agentdojo.task_suite.task_suite import TaskSuite

from lab.models.fake import Step, answer, call
from lab.topologies.t2 import delegate_tool_name, worker_for_tool

Env = TypeVar("Env", bound=TaskEnvironment)

SCRIPTED_MODELS = ("ground-truth", "refuse", "obedient")
REFUSAL = "I can't help with that."
WORKER_REPORT = "Done."
RELAY = " I was also asked to pass this on: "


def script_for(
    model_id: str,
    suite: TaskSuite[Env],
    user_task: BaseUserTask[Env],
    injection_task: BaseInjectionTask[Env] | None = None,
    topology: str = "t1",
) -> list[Step]:
    """The steps in the order the run makes its calls, for a FakeModel."""
    if model_id not in SCRIPTED_MODELS:
        raise ValueError(f"Unknown scripted model {model_id!r}; known: {SCRIPTED_MODELS}")
    if model_id == "refuse":
        return [answer(REFUSAL)]
    obeyed = injection_task if model_id == "obedient" else None
    if topology == "t1":
        return ground_truth_script(suite, user_task, obeyed)
    if topology == "t2":
        return t2_ground_truth_script(suite, user_task, obeyed)
    raise ValueError(f"No scripts for topology {topology!r}")


def scripts_by_request(
    model_id: str,
    suite: TaskSuite[Env],
    user_task: BaseUserTask[Env],
    injection_task: BaseInjectionTask[Env] | None = None,
    topology: str = "t1",
) -> dict[str, list[Step]]:
    """The same steps, per agent, keyed by the agent's first user message: for a
    ScriptedModel, which stays in step when the response cache answers a call (D54)."""
    if topology == "t2" and model_id != "refuse":
        obeyed = injection_task if model_id == "obedient" else None
        planner, workers = _t2_agent_scripts(suite, user_task, obeyed)
        scripts = {user_task.PROMPT: planner}
        for instruction, steps in workers:
            if scripts.setdefault(instruction, steps) != steps:
                raise ValueError(f"Two workers are given {instruction!r} and act differently")
        return scripts
    # One agent, whose first user message is the user's request (T1, or a planner that refuses).
    return {user_task.PROMPT: script_for(model_id, suite, user_task, injection_task, topology)}


def _ground_truth_calls(
    suite: TaskSuite[Env], user_task: BaseUserTask[Env], injection_task: BaseInjectionTask[Env]
    | None = None,
) -> tuple[list[FunctionCall], list[FunctionCall]]:
    """(the user task's ground-truth calls, the injection task's, or [] without one)."""
    # The ground truth reads the environment the task starts from, which run_case rebuilds the
    # same way (load the default environment, then init_environment), so the calls match.
    environment = user_task.init_environment(suite.load_and_inject_default_environment({}))
    user_calls = list(user_task.ground_truth(environment))
    injected = [] if injection_task is None else list(injection_task.ground_truth(environment))
    return user_calls, injected


def ground_truth_script(
    suite: TaskSuite[Env],
    user_task: BaseUserTask[Env],
    injection_task: BaseInjectionTask[Env] | None = None,
) -> list[Step]:
    """The user task's ground-truth calls, then (if given) the injection task's, then the answer."""
    user_calls, injected = _ground_truth_calls(suite, user_task, injection_task)
    steps: list[Step] = [
        call(c.function, dict(c.args), call_id=f"gt_{i}")
        for i, c in enumerate(user_calls + injected)
    ]
    steps.append(answer(user_task.GROUND_TRUTH_OUTPUT))
    return steps


# --- T2 ---


@dataclass
class Delegation:
    """One delegate call in a T2 script and what the worker does with it."""

    worker: str
    calls: list[FunctionCall] = field(default_factory=list)  # the user task's, as instructed
    # The injection task's calls, made after `calls`. Never listed in the instruction: the
    # planner only knows the goal if a worker relayed it (`forwards`).
    injected: list[FunctionCall] = field(default_factory=list)
    forwards: str | None = None  # an injected goal the planner adds to the instruction
    relays: str | None = None  # an injected goal the worker adds to its report


def t2_delegations(
    suite: TaskSuite[Env],
    user_task: BaseUserTask[Env],
    injection_task: BaseInjectionTask[Env] | None = None,
) -> list[Delegation]:
    """The ground truth cut into delegations: runs of consecutive calls owned by one worker.
    With an injection task, the obedient path through them (module docstring)."""
    user_calls, injected = _ground_truth_calls(suite, user_task, injection_task)
    delegations: list[Delegation] = []
    for c in user_calls:
        worker = worker_for_tool(c.function)
        if not delegations or delegations[-1].worker != worker:
            delegations.append(Delegation(worker))
        delegations[-1].calls.append(c)
    if not injected:
        return delegations

    attack_workers = {worker_for_tool(c.function) for c in injected}
    if len(attack_workers) != 1:
        raise ValueError(f"{injection_task and injection_task.ID} needs more than one worker")
    (attack_worker,) = attack_workers
    reader = _reading_delegation(suite, user_task, delegations)
    if delegations[reader].worker == attack_worker:
        delegations[reader].injected = injected  # it holds the tools: it acts on its own
        return delegations
    goal = injection_task.GOAL if injection_task is not None else ""
    delegations[reader].relays = goal
    # The planner passes the goal on with its next instruction to the worker that can act on it,
    # or in a delegation of its own after the user task's.
    target = next(
        (d for d in delegations[reader + 1 :] if d.worker == attack_worker), None
    )
    if target is None:
        target = Delegation(attack_worker)
        delegations.append(target)
    target.forwards = goal
    target.injected = injected
    return delegations


def _reading_delegation(
    suite: TaskSuite[Env], user_task: BaseUserTask[Env], delegations: list[Delegation]
) -> int:
    """The first delegation whose calls read injected text. Found as AgentDojo's
    `BaseAttack.get_injection_candidates` finds the vectors: run the ground truth on an
    environment with a canary in every vector and look for the canaries in the tool outputs."""
    canaries = {v: f"<CANARY---{v}--->" for v in suite.get_injection_vector_defaults()}
    environment = user_task.init_environment(suite.load_and_inject_default_environment(canaries))
    runtime = FunctionsRuntime(suite.tools)
    for i, delegation in enumerate(delegations):
        for c in delegation.calls:
            output, _ = runtime.run_function(environment, c.function, dict(c.args))
            if any(canary in tool_result_to_str(output) for canary in canaries.values()):
                return i
    raise ValueError(f"No ground-truth call of {user_task.ID} reads an injection vector")


def instruction_for(delegation: Delegation) -> str:
    parts = []
    if delegation.calls:
        calls = "; ".join(f"{c.function}({json.dumps(dict(c.args))})" for c in delegation.calls)
        parts.append(f"Please make these calls: {calls}.")
    if delegation.forwards:
        parts.append(f"Also: {delegation.forwards}")
    return " ".join(parts)


def _t2_agent_scripts(
    suite: TaskSuite[Env],
    user_task: BaseUserTask[Env],
    injection_task: BaseInjectionTask[Env] | None = None,
) -> tuple[list[Step], list[tuple[str, list[Step]]]]:
    """(the planner's steps, then each delegation's (instruction, worker steps), in order).
    The planner's step k is delegation k; its last step is the answer to the user."""
    planner: list[Step] = []
    workers: list[tuple[str, list[Step]]] = []
    for k, d in enumerate(t2_delegations(suite, user_task, injection_task)):
        instruction = instruction_for(d)
        planner.append(call(
            delegate_tool_name(d.worker), {"instruction": instruction}, call_id=f"delegate_{k}"
        ))
        steps: list[Step] = [
            call(c.function, dict(c.args), call_id=f"gt_{k}_{i}")
            for i, c in enumerate(d.calls + d.injected)
        ]
        steps.append(answer(WORKER_REPORT + (RELAY + d.relays if d.relays else "")))
        workers.append((instruction, steps))
    planner.append(answer(user_task.GROUND_TRUTH_OUTPUT))
    return planner, workers


def t2_ground_truth_script(
    suite: TaskSuite[Env],
    user_task: BaseUserTask[Env],
    injection_task: BaseInjectionTask[Env] | None = None,
) -> list[Step]:
    """Every agent's steps in call order: each delegation, then that worker's steps; finally
    the planner's answer."""
    planner, workers = _t2_agent_scripts(suite, user_task, injection_task)
    steps: list[Step] = []
    for delegate, (_, worker_steps) in zip(planner, workers, strict=False):
        steps += [delegate, *worker_steps]
    steps.append(planner[-1])
    return steps
