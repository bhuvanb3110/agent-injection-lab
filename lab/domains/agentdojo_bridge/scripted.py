"""FakeModel scripts derived from AgentDojo tasks, for the smoke run and tests (D33).

Two scripted "models", named by model ID under provider "fake":
- `ground-truth`: makes the task's ground-truth tool calls one per turn, then gives the task's
  GROUND_TRUTH_OUTPUT as the answer. Same steps as AgentDojo's GroundTruthPipeline
  (agentdojo/agent_pipeline/ground_truth_pipeline.py), but through LabLLM, the cache, the
  scheduler and LabToolsExecutor, so the smoke run exercises the real code path.
- `refuse`: answers at once without calling a tool. Solves nothing that changes state.
"""

from __future__ import annotations

from typing import TypeVar

from agentdojo.base_tasks import BaseUserTask
from agentdojo.functions_runtime import TaskEnvironment
from agentdojo.task_suite.task_suite import TaskSuite

from lab.models.fake import Step, answer, call

Env = TypeVar("Env", bound=TaskEnvironment)

SCRIPTED_MODELS = ("ground-truth", "refuse")


def script_for(model_id: str, suite: TaskSuite[Env], user_task: BaseUserTask[Env]) -> list[Step]:
    if model_id == "ground-truth":
        return ground_truth_script(suite, user_task)
    if model_id == "refuse":
        return [answer("I can't help with that.")]
    raise ValueError(f"Unknown scripted model {model_id!r}; known: {SCRIPTED_MODELS}")


def ground_truth_script(suite: TaskSuite[Env], user_task: BaseUserTask[Env]) -> list[Step]:
    # The ground truth reads the environment the task starts from, which run_case rebuilds the
    # same way (load the default environment, then init_environment), so the calls match.
    environment = user_task.init_environment(suite.load_and_inject_default_environment({}))
    steps: list[Step] = [
        call(c.function, dict(c.args), call_id=f"gt_{i}")
        for i, c in enumerate(user_task.ground_truth(environment))
    ]
    steps.append(answer(user_task.GROUND_TRUTH_OUTPUT))
    return steps
