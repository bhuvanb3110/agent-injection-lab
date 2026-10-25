"""T1, the single agent: one model with all of the suite's tools (BRIEF §4, DECISIONS.md D4).

It is AgentDojo's default pipeline shape (agentdojo/agent_pipeline/agent_pipeline.py:
AgentPipeline.from_config) with two parts swapped for ours:

    SystemMessage -> InitQuery -> LabLLM -> ToolsExecutionLoop([LabToolsExecutor, LabLLM])

- `LabLLM` instead of `OpenAILLM` (D1, D14).
- `LabToolsExecutor` instead of `ToolsExecutor`, so malformed tool calls are never run (D15).

The loop stops when the model answers without tool calls, or after `max_iters` rounds, which
run_case records as `max_iterations`.
"""

from __future__ import annotations

from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, load_system_message
from agentdojo.agent_pipeline.basic_elements import InitQuery, SystemMessage
from agentdojo.agent_pipeline.tool_execution import ToolsExecutionLoop

from lab.models.llm_element import LabLLM, LabToolsExecutor

NAME = "t1"
# ToolsExecutionLoop's own default (agentdojo/agent_pipeline/tool_execution.py), written out so
# a change upstream can't change our runs silently.
MAX_ITERS = 15


def default_system_message() -> str:
    """AgentDojo's "default" system message (agentdojo/data/system_messages.yaml)."""
    return load_system_message(None)


def build_t1(
    llm: LabLLM, *, system_message: str | None = None, max_iters: int = MAX_ITERS
) -> AgentPipeline:
    """The T1 pipeline around `llm`. The system message defaults to AgentDojo's."""
    message = system_message if system_message is not None else default_system_message()
    pipeline = AgentPipeline(
        [
            SystemMessage(message),
            InitQuery(),
            llm,
            ToolsExecutionLoop([LabToolsExecutor(), llm], max_iters=max_iters),
        ]
    )
    pipeline.name = NAME
    return pipeline
