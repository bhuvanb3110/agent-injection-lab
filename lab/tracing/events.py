"""Trace events: one per message or handoff, for every agent of a run (PLAN_M2 §8).

Every message in every agent's transcript becomes a `message` event, T1's included, so the
analysis is the same for all topologies. Handoffs between agents are their own events (T2):
- `delegation`: the planner hands a worker an instruction (`agent_id` is the planner);
- `result`: the worker's final message goes back to the planner (`agent_id` is the worker).
The same text also shows up as a message in the receiving transcript (the worker's user turn,
the planner's `delegate` tool result). Those copies are traced too, but only the handoff event
is searched for markers. Oversight decisions come with slice 4.

Each event says whose transcript it belongs to (`agent_id`, `agent_role`), who wrote it
(`from_agent`) and who receives it (`to_agent`). The pseudo-agents `system`, `user` and `tools`
stand for the system prompt, the user's request and the environment; `workers` is where the
planner's delegate calls go. Marker hops are counted along these edges (lab/tracing/markers.py).
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
# Where the planner's tool calls go: each one is a delegation, traced as its own event.
WORKERS = "workers"


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
    seq: int,
    agent_id: str,
    agent_role: str,
    message: ChatMessage,
    call_idx: int | None,
    *,
    principal: str = USER,
    tool_target: str = TOOLS,
    sender: str | None = None,
) -> TraceEvent:
    """A message in `agent_id`'s transcript, with its sender and receiver read off its role.

    `principal` is whom the agent works for: its user turn comes from them and its final answer
    (an assistant message without tool calls) goes to them. `tool_target` is where its tool
    calls go. For T1 these are the user and the tools; a T2 worker's principal is the planner,
    and the planner's tool calls go to the workers. `sender` overrides the sender of a tool
    result, for the planner's `delegate` results, which a worker wrote.
    """
    if message["role"] == "assistant":
        sender = agent_id
        receiver = tool_target if message["tool_calls"] else principal
    else:
        if sender is None:
            sender = {"system": SYSTEM, "user": principal, "tool": TOOLS}[message["role"]]
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


def delegation_event(
    seq: int, planner: str, worker: str, worker_role: str, tool_call_id: str | None,
    instruction: str,
) -> TraceEvent:
    """The planner hands `worker` an instruction. It becomes the worker's user turn."""
    return TraceEvent(
        seq=seq,
        agent_id=planner,
        agent_role="planner",
        kind="delegation",
        from_agent=planner,
        to_agent=worker,
        call_idx=None,
        payload={"worker": worker_role, "tool_call_id": tool_call_id, "instruction": instruction},
    )


def result_event(
    seq: int, worker: str, worker_role: str, planner: str, tool_call_id: str | None,
    report: str, error: str | None,
) -> TraceEvent:
    """`worker`'s final message goes back to the planner as its `delegate` tool result.
    `error` is set, and `report` empty, when the worker ended without a final message."""
    return TraceEvent(
        seq=seq,
        agent_id=worker,
        agent_role=worker_role,
        kind="result",
        from_agent=worker,
        to_agent=planner,
        call_idx=None,
        payload={
            "worker": worker_role, "tool_call_id": tool_call_id, "report": report, "error": error,
        },
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
