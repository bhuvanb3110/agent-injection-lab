"""Trace events: one per message or handoff, for every agent of a run (PLAN_M2 §8).

Every message in every agent's transcript becomes a `message` event, T1's included, so the
analysis is the same for all topologies. Handoffs between agents are their own events (T2):
- `delegation`: the planner hands a worker an instruction (`agent_id` is the planner);
- `result`: the worker's final message goes back to the planner (`agent_id` is the worker).
The same text also shows up as a message in the receiving transcript (the worker's user turn,
the planner's `delegate` tool result). Those copies are traced too, but only the handoff event
is searched for markers.

Oversight decisions (T3, T4) are `review_verdict` events: one per gate per check, from the agent
that decided (`reviewer#k`, or `policy`) to the agent whose input the verdict decides (the
planner at the handoff checkpoint, the worker at the action checkpoint). The payload says what
was checked (`sources`, the events whose text the gate was shown) and what the recipient gets
because of the verdict (`delivered`): the report passed on unchanged, a "withheld" notice, or a
"blocked" tool error. A report waiting for its verdict is addressed to `checkpoint`.

The simulated human of T5 and T5-summary answers through its own pair of events: the worker's
`approval_request` (what the human is shown: the raw call, or the worker's description of it)
and the human's `approval_decision` back to the worker.

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
# Where a gated report waits for its verdict, and where a reviewer's reply goes: the checkpoint
# code reads it and records the outcome as a `review_verdict` event.
CHECKPOINT = "checkpoint"
# The role of T3's LLM reviewers (`reviewer#k`). The markers' `reviewer_exposed` looks for it.
REVIEWER = "reviewer"


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


def review_verdict_event(
    seq: int,
    agent_id: str,
    agent_role: str,
    recipient: str,
    call_idx: int | None,
    *,
    gate: str,
    checkpoint: str,
    approve: bool,
    reason: str,
    unparseable: bool,
    output: str | None,
    tool_call_id: str | None,
    call: dict[str, Any] | None,
    sources: list[int],
    forwards: int | None,
    delivered: str | None,
) -> TraceEvent:
    """One gate's verdict on one check (lab/defenses/gates.py).

    - `output`: what a model gate replied, verbatim (None for the policy, which writes nothing);
    - `sources`: the seqs of the events whose text the gate was shown;
    - `forwards`: on an approved handoff, the seq of the `result` passed on unchanged;
    - `delivered`: what `recipient` gets because of this verdict: the report itself (approved
      handoff), the withheld notice or the block error (rejected), None for an approved call,
      whose result comes from the tools as usual.
    """
    return TraceEvent(
        seq=seq,
        agent_id=agent_id,
        agent_role=agent_role,
        kind="review_verdict",
        from_agent=agent_id,
        to_agent=recipient,
        call_idx=call_idx,
        payload={
            "gate": gate,
            "checkpoint": checkpoint,
            "verdict": "approve" if approve else "reject",
            "reason": reason,
            "unparseable": unparseable,
            "output": output,
            "tool_call_id": tool_call_id,
            "call": call,
            "sources": sources,
            "forwards": forwards,
            "delivered": delivered,
        },
    )


def approval_request_event(
    seq: int,
    worker: str,
    worker_role: str,
    human: str,
    call_idx: int | None,
    *,
    gate: str,
    mode: str,
    tool_call_id: str | None,
    call: dict[str, Any],
    text: str,
    sources: list[int],
    forwards: int | None,
) -> TraceEvent:
    """A write call waits for the simulated human's approval (T5, T5-summary; D64).

    - `text`: exactly what the human is shown. `raw`: the call itself, as `function({args})`.
      `summary`: the description the proposing worker wrote in a side call (`call_idx`);
    - `forwards`: for `raw`, the seq of the worker's message whose call this is, since the text
      is a copy of it; None for `summary`, which the worker wrote anew;
    - `sources`: the events the request comes from (the delegation and the call).
    It is the worker's request, so `agent_id` is the worker and it goes to the human.
    """
    return TraceEvent(
        seq=seq,
        agent_id=worker,
        agent_role=worker_role,
        kind="approval_request",
        from_agent=worker,
        to_agent=human,
        call_idx=call_idx,
        payload={
            "gate": gate,
            "mode": mode,
            "tool_call_id": tool_call_id,
            "call": call,
            "text": text,
            "sources": sources,
            "forwards": forwards,
        },
    )


def approval_decision_event(
    seq: int,
    human: str,
    recipient: str,
    *,
    gate: str,
    request: int,
    approve: bool,
    reason: str,
    unparseable: bool,
    tool_call_id: str | None,
    reading: dict[str, Any] | None,
    delivered: str | None,
) -> TraceEvent:
    """The human's answer to the approval request at seq `request` (D64). The human makes no
    model call and writes only fixed reasons. `reading`: what T5-summary's parser found in the
    summary (None for `raw`). `delivered`: the block error the worker gets, None on approval."""
    return TraceEvent(
        seq=seq,
        agent_id=human,
        agent_role=human,
        kind="approval_decision",
        from_agent=human,
        to_agent=recipient,
        call_idx=None,
        payload={
            "gate": gate,
            "request": request,
            "verdict": "approve" if approve else "reject",
            "reason": reason,
            "unparseable": unparseable,
            "tool_call_id": tool_call_id,
            "reading": reading,
            "sources": [request],
            "forwards": None,
            "delivered": delivered,
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
