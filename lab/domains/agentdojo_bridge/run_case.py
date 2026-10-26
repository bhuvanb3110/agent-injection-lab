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

With an injection task the case is an attack: the payload is put into the environment before
the run, and `attack_succeeded` is the injection task's own check (AgentDojo calls it
`security`, but it returns True when the *attack* worked, so we don't reuse the name; see
`TaskSuite.run_task_with_pipeline`). It is scored for every run that reaches the store, model
errors included (D49).

The function trace given to the `*_from_traces` checks is every tool call any agent sent to the
tools, in the order they were made (PLAN_M2 §6). In T1 that is the agent's own calls, exactly
what AgentDojo reads off the transcript. In T2 it is the workers' calls; the planner's delegate
calls are not tool calls on the environment and are left out.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.base_tasks import BaseInjectionTask, BaseUserTask
from agentdojo.functions_runtime import FunctionCall, FunctionsRuntime, TaskEnvironment
from agentdojo.task_suite.task_suite import TaskSuite, functions_stack_trace_from_messages
from agentdojo.types import ChatAssistantMessage, ChatMessage, get_text_content_as_str

from lab.errors import ModelError
from lab.models.types import CallRecord, RunContext
from lab.tracing.events import TOOLS, TraceEvent

Env = TypeVar("Env", bound=TaskEnvironment)


class Outcome(StrEnum):
    SOLVED = "solved"
    NOT_SOLVED = "not_solved"
    # Model errors: count as not solved, reported as a separate breakdown.
    MAX_ITERATIONS = "max_iterations"  # stopped with tool calls still pending
    NO_FINAL_ANSWER = "no_final_answer"  # final assistant message has content=None
    # The provider raised ModelError mid-run. Never returned by run_case (the error
    # propagates); the scheduler records it, with a subtype (D21).
    MODEL_ERROR = "model_error"


@dataclass(frozen=True)
class CaseResult:
    user_task_id: str
    outcome: Outcome
    utility: bool  # True only when outcome is SOLVED
    state_check_passed: bool  # the task's utility predicate, evaluated for every outcome
    messages: list[ChatMessage]
    calls: list[CallRecord]  # one per model call, in order (D17)
    malformed_tool_calls: int  # D6
    attack_succeeded: bool | None = None  # None: no injection task, so nothing to score (D49)


def run_case(
    suite: TaskSuite[Env],
    pipeline: BasePipelineElement,
    user_task: BaseUserTask[Env],
    ctx: RunContext | None = None,
    *,
    injections: dict[str, str] | None = None,
    injection_task: BaseInjectionTask[Env] | None = None,
) -> CaseResult:
    """Run `user_task` once and score the final state and answer.

    With `injections` (vector -> payload) and `injection_task` it is an attack case and
    `attack_succeeded` is scored as well; with neither, a benign run.

    `ctx` collects a record of every model call. The runner passes one carrying the repeat
    index; tests can leave it out. If the run ends in ModelError, `ctx.attack_succeeded` holds
    the attack score before the error is re-raised, because the tools had already changed the
    environment (D49).
    """
    if (injections is None) != (injection_task is None):
        raise ValueError("injections and injection_task go together")
    ctx = ctx if ctx is not None else RunContext()
    environment = suite.load_and_inject_default_environment(injections or {})
    task_environment = user_task.init_environment(environment)
    pre_environment = task_environment.model_copy(deep=True)
    runtime = FunctionsRuntime(suite.tools)

    # Pass messages and extra_args explicitly: the defaults in AgentDojo's signatures are
    # shared mutable objects.
    try:
        _, _, post_environment, result_messages, _ = pipeline.query(
            user_task.PROMPT, runtime, task_environment, [], {"ctx": ctx}
        )
    except ModelError:
        if injection_task is not None:
            # The pipeline raised before returning, but tools mutate `task_environment` in
            # place, so a transfer made before the error did happen. The output is empty and
            # the last assistant message, never executed, is not part of the trace.
            trace = tool_call_trace(ctx.events, ran_to_completion=False)
            ctx.attack_succeeded = _check_attack(
                injection_task, "", trace, pre_environment, task_environment
            )
        raise
    messages = list(result_messages)
    final = _final_assistant_message(messages)
    # A pipeline without LabLLM (AgentDojo's GroundTruthPipeline, in the parity test) keeps no
    # trace events; its one transcript is then the trace, as in AgentDojo.
    trace = (
        tool_call_trace(ctx.events)
        if ctx.events
        else functions_stack_trace_from_messages(messages)
    )

    state_check_passed = _check_utility(
        user_task, final, trace, pre_environment, post_environment
    )
    outcome = _classify(final, state_check_passed)
    if injection_task is not None:
        ctx.attack_succeeded = _check_attack(
            injection_task,
            get_text_content_as_str(final["content"] or []),
            trace,
            pre_environment,
            post_environment,
        )
    return CaseResult(
        user_task_id=user_task.ID,
        outcome=outcome,
        utility=outcome is Outcome.SOLVED,
        state_check_passed=state_check_passed,
        messages=messages,
        calls=list(ctx.calls),
        malformed_tool_calls=ctx.malformed_tool_calls,
        attack_succeeded=ctx.attack_succeeded,
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


def tool_call_trace(
    events: Sequence[TraceEvent], *, ran_to_completion: bool = True
) -> list[FunctionCall]:
    """Every tool call any agent sent to the tools, in the order the calls were made.

    Like AgentDojo's `functions_stack_trace_from_messages`, it includes calls in a final message
    that were never run (a run stopped at max_iterations). After a ModelError
    (`ran_to_completion=False`), the run's last event, if it is a model's message, is left out:
    the run broke off before its calls could run (D49).
    """
    ordered = sorted(events, key=lambda e: e.seq)
    if not ran_to_completion and ordered and ordered[-1].payload.get("role") == "assistant":
        ordered = ordered[:-1]
    return [
        FunctionCall.model_validate(call)
        for event in ordered
        if event.kind == "message" and event.payload["role"] == "assistant"
        and event.to_agent == TOOLS
        for call in event.payload["tool_calls"] or []
    ]


def _check_utility(
    user_task: BaseUserTask[Env],
    final: ChatAssistantMessage,
    traces: list[FunctionCall],
    pre_environment: Env,
    post_environment: Env,
) -> bool:
    # Same order as TaskSuite._check_user_task_utility: the trace-based check wins if defined.
    output_text = get_text_content_as_str(final["content"] or [])
    from_traces = user_task.utility_from_traces(
        output_text, pre_environment, post_environment, traces
    )
    if from_traces is not None:
        return from_traces
    return user_task.utility(output_text, pre_environment, post_environment)


def _check_attack(
    injection_task: BaseInjectionTask[Env],
    output_text: str,
    traces: list[FunctionCall],
    pre_environment: Env,
    post_environment: Env,
) -> bool:
    # Same order as TaskSuite._check_injection_task_security: the trace-based check wins if
    # defined. True means the attack worked.
    from_traces = injection_task.security_from_traces(
        output_text, pre_environment, post_environment, traces
    )
    if from_traces is not None:
        return from_traces
    return injection_task.security(output_text, pre_environment, post_environment)


def _classify(final: ChatAssistantMessage, state_check_passed: bool) -> Outcome:
    if final["tool_calls"]:
        return Outcome.MAX_ITERATIONS
    if final["content"] is None:
        return Outcome.NO_FINAL_ANSWER
    return Outcome.SOLVED if state_check_passed else Outcome.NOT_SOLVED
