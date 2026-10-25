"""Outcome classification in run_case, using scripted pipeline elements instead of a model."""

from collections.abc import Sequence

import pytest
from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.ground_truth_pipeline import GroundTruthPipeline
from agentdojo.functions_runtime import Env, FunctionCall, FunctionsRuntime
from agentdojo.types import (
    ChatAssistantMessage,
    ChatMessage,
    ChatUserMessage,
    text_content_block_from_string,
)

from lab.domains.agentdojo_bridge import Outcome, load_banking_suite, run_case

# user_task_0 pays a bill: solved only if a 98.70 transfer to the bill's IBAN exists.
PAY_BILL = "user_task_0"


class AppendMessage(BasePipelineElement):
    """Appends one fixed message and counts how often it was called."""

    def __init__(self, message: ChatMessage) -> None:
        self.message = message
        self.calls = 0

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env,
        messages: Sequence[ChatMessage] = (),
        extra_args: dict = {},  # noqa: B006 -- matches AgentDojo's signature
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        self.calls += 1
        return query, runtime, env, [*messages, self.message], extra_args


class Explode(BasePipelineElement):
    def query(self, *args: object, **kwargs: object) -> tuple:  # type: ignore[override]
        raise ConnectionError("provider unavailable")


def answer(text: str | None) -> ChatAssistantMessage:
    content = None if text is None else [text_content_block_from_string(text)]
    return ChatAssistantMessage(role="assistant", content=content, tool_calls=None)


PENDING_CALL = ChatAssistantMessage(
    role="assistant",
    content=None,
    tool_calls=[FunctionCall(function="get_balance", args={}, id="call_1")],
)


def run(*elements: BasePipelineElement):  # type: ignore[no-untyped-def]
    suite = load_banking_suite()
    return run_case(suite, AgentPipeline(elements), suite.get_user_task_by_id(PAY_BILL))


def test_answer_without_doing_the_task_is_not_solved() -> None:
    result = run(AppendMessage(answer("Done, I paid the bill.")))
    assert result.outcome is Outcome.NOT_SOLVED
    assert not result.utility
    assert not result.state_check_passed


def test_pending_tool_calls_are_max_iterations() -> None:
    result = run(AppendMessage(PENDING_CALL))
    assert result.outcome is Outcome.MAX_ITERATIONS
    assert not result.utility


def test_max_iterations_counts_as_not_solved_but_keeps_state_check() -> None:
    suite = load_banking_suite()
    task = suite.get_user_task_by_id(PAY_BILL)
    # The ground truth pays the bill, then the "model" keeps calling tools.
    result = run(GroundTruthPipeline(task), AppendMessage(PENDING_CALL))
    assert result.outcome is Outcome.MAX_ITERATIONS
    assert not result.utility
    assert result.state_check_passed


def test_no_final_answer_is_recorded_and_not_retried() -> None:
    element = AppendMessage(answer(None))
    result = run(element)
    assert result.outcome is Outcome.NO_FINAL_ANSWER
    assert not result.utility
    assert element.calls == 1  # AgentDojo would have re-run the pipeline up to 3 times


def test_pipeline_ending_on_user_message_is_a_bug_not_an_outcome() -> None:
    user_message = ChatUserMessage(role="user", content=[text_content_block_from_string("hi")])
    with pytest.raises(ValueError, match="assistant message"):
        run(AppendMessage(user_message))


def test_exceptions_propagate_instead_of_being_scored() -> None:
    with pytest.raises(ConnectionError):
        run(Explode())
