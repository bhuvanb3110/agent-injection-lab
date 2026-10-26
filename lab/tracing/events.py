"""Trace events: one per message or handoff, for every agent of a run (PLAN_M2 §8).

Every message in every agent's transcript becomes a `message` event, T1's included, so the
analysis is the same for all topologies. Handoffs between agents (`delegation`, `result`) and
oversight decisions come with the topologies that have them (slices 3 and 4).

Each event says whose transcript it belongs to (`agent_id`, `agent_role`), who wrote it
(`from_agent`) and who receives it (`to_agent`). The pseudo-agents `system`, `user` and `tools`
stand for the system prompt, the user's request and the environment. Marker hops are counted
along these edges (lab/tracing/markers.py).
"""

from __future__ import annotations

from typing import Any, Literal

from agentdojo.types import ChatMessage
from pydantic import BaseModel, ConfigDict

EventKind = Literal[
    "message", "delegation", "result", "review_verdict", "approval_request", "approval_decision"
]

# T1's one agent. A LabLLM that isn't told otherwise is this agent.
SINGLE_AGENT = "agent"
SINGLE_ROLE = "single"

# Pseudo-agents: where messages that no agent wrote come from, and where a final answer goes.
SYSTEM = "system"
USER = "user"
TOOLS = "tools"


class TraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int  # position in the run, over all agents; the order things happened
    agent_id: str  # whose transcript (or, for a handoff, which agent sent it)
    agent_role: str
    kind: EventKind
    from_agent: str
    to_agent: str
    call_idx: int | None  # the row in `calls` that produced it; None if no model call did
    payload: dict[str, Any]  # JSON-safe: the message itself, or the handoff's content


def message_event(
    seq: int, agent_id: str, agent_role: str, message: ChatMessage, call_idx: int | None
) -> TraceEvent:
    """A message in `agent_id`'s transcript, with its sender and receiver read off its role.

    An assistant message with tool calls goes to the tools; one without goes to the user. For
    T1 that is exactly right. Workers, whose final message goes to the planner, come in slice 3.
    """
    if message["role"] == "assistant":
        sender = agent_id
        receiver = TOOLS if message["tool_calls"] else USER
    else:
        sender = {"system": SYSTEM, "user": USER, "tool": TOOLS}[message["role"]]
        receiver = agent_id
    return TraceEvent(
        seq=seq,
        agent_id=agent_id,
        agent_role=agent_role,
        kind="message",
        from_agent=sender,
        to_agent=receiver,
        call_idx=call_idx,
        payload=json_safe(message),
    )


def json_safe(value: Any) -> Any:
    """AgentDojo messages are TypedDicts holding pydantic FunctionCalls; make them plain JSON."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    return value
