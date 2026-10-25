"""The do-nothing floor (D37, D39): which Banking tasks pass for an agent that takes no action.

The report's discriminating-task utility and floor rest on DO_NOTHING_PASSES. This test derives
the set from AgentDojo's own utility predicates, so a change to the installed tasks fails here
instead of silently moving the headline number.
"""

from collections.abc import Sequence

from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.basic_elements import InitQuery
from agentdojo.functions_runtime import Env, FunctionsRuntime
from agentdojo.types import ChatAssistantMessage, ChatMessage, text_content_block_from_string

from lab.domains.agentdojo_bridge import (
    DO_NOTHING_PASSES,
    banking_task_ids,
    load_banking_suite,
    run_case,
)


class DoNothing(BasePipelineElement):
    """Answers at once with no tool call: the same behaviour as the `refuse` script (D33)."""

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env,
        messages: Sequence[ChatMessage] = (),
        extra_args: dict = {},  # noqa: B006 -- matches AgentDojo's signature
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        reply = ChatAssistantMessage(
            role="assistant",
            content=[text_content_block_from_string("I can't help with that.")],
            tool_calls=None,
        )
        return query, runtime, env, [*messages, reply], extra_args


def test_do_nothing_agent_passes_exactly_the_pinned_tasks() -> None:
    suite = load_banking_suite()
    passed = set()
    for task_id in banking_task_ids():
        task = suite.get_user_task_by_id(task_id)
        if run_case(suite, AgentPipeline([InitQuery(), DoNothing()]), task).utility:
            passed.add(task_id)
    assert passed == DO_NOTHING_PASSES
    assert len(banking_task_ids()) - len(passed) == 11  # the discriminating tasks
