"""Run one AgentDojo user task through a pipeline, exactly once, and score it.

This mirrors `TaskSuite.run_task_with_pipeline` (agentdojo/task_suite/task_suite.py) and
scores with the task's own `utility_from_traces` / `utility` methods, so a solved case means
the same thing here as in AgentDojo. Three deliberate differences:

- One attempt only. AgentDojo re-runs the pipeline up to 3 times on the already-changed
  environment when the final output is None; we record that as NO_FINAL_ANSWER (DECISIONS.md D7).
- Model errors get their own outcome and count as not solved (D5). The raw state check is kept
  in `state_check_passed` so the analysis can still see it (D9).
- Exceptions are never turned into a score. Infrastructure errors and bugs propagate to the
  caller, which re-queues or crashes (D5).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.base_tasks import BaseUserTask
from agentdojo.functions_runtime import FunctionsRuntime, TaskEnvironment
from agentdojo.task_suite.task_suite import TaskSuite, functions_stack_trace_from_messages
from agentdojo.types import ChatAssistantMessage, ChatMessage, get_text_content_as_str

Env = TypeVar("Env", bound=TaskEnvironment)


class Outcome(StrEnum):
    SOLVED = "solved"
    NOT_SOLVED = "not_solved"
    # Model errors: count as not solved, reported as a separate breakdown.
    MAX_ITERATIONS = "max_iterations"  # stopped with tool calls still pending
    NO_FINAL_ANSWER = "no_final_answer"  # final assistant message has content=None


@dataclass(frozen=True)
class CaseResult:
    user_task_id: str
    outcome: Outcome
    utility: bool  # True only when outcome is SOLVED
    state_check_passed: bool  # the task's utility predicate, evaluated for every outcome
    messages: list[ChatMessage]


def run_case(
    suite: TaskSuite[Env],
    pipeline: BasePipelineElement,
    user_task: BaseUserTask[Env],
) -> CaseResult:
    """Run `user_task` once with no injections and score the final state and answer."""
    environment = suite.load_and_inject_default_environment({})
    task_environment = user_task.init_environment(environment)
    pre_environment = task_environment.model_copy(deep=True)
    runtime = FunctionsRuntime(suite.tools)

    # Pass messages and extra_args explicitly: the defaults in AgentDojo's signatures are
    # shared mutable objects.
    _, _, post_environment, result_messages, _ = pipeline.query(
        user_task.PROMPT, runtime, task_environment, [], {}
    )
    messages = list(result_messages)
    final = _final_assistant_message(messages)

    state_check_passed = _check_utility(
        user_task, final, messages, pre_environment, post_environment
    )
    outcome = _classify(final, state_check_passed)
    return CaseResult(
        user_task_id=user_task.ID,
        outcome=outcome,
        utility=outcome is Outcome.SOLVED,
        state_check_passed=state_check_passed,
        messages=messages,
    )


def _final_assistant_message(messages: Sequence[ChatMessage]) -> ChatAssistantMessage:
    # Every pipeline must end on an assistant turn; anything else is a bug in the pipeline,
    # not a model outcome, so it raises instead of being scored.
    if not messages:
        raise ValueError("Pipeline returned no messages")
    last = messages[-1]
    if last["role"] != "assistant":
        raise ValueError(f"Pipeline must end with an assistant message, got role={last['role']!r}")
    return last


def _check_utility(
    user_task: BaseUserTask[Env],
    final: ChatAssistantMessage,
    messages: Sequence[ChatMessage],
    pre_environment: Env,
    post_environment: Env,
) -> bool:
    # Same order as TaskSuite._check_user_task_utility: the trace-based check wins if defined.
    output_text = get_text_content_as_str(final["content"] or [])
    traces = functions_stack_trace_from_messages(messages)
    from_traces = user_task.utility_from_traces(
        output_text, pre_environment, post_environment, traces
    )
    if from_traces is not None:
        return from_traces
    return user_task.utility(output_text, pre_environment, post_environment)


def _classify(final: ChatAssistantMessage, state_check_passed: bool) -> Outcome:
    if final["tool_calls"]:
        return Outcome.MAX_ITERATIONS
    if final["content"] is None:
        return Outcome.NO_FINAL_ANSWER
    return Outcome.SOLVED if state_check_passed else Outcome.NOT_SOLVED
