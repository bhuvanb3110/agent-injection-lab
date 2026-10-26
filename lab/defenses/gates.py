"""Gates and the two checkpoints they sit at (BRIEF §4 "Defense", PLAN_M2 Q2-Q3).

A gate is one oversight mechanism (the LLM reviewer, the policy engine). It is written once and
attached at named checkpoints; a topology preset says which (lab/topologies/presets.py).

- **handoff**: a worker's report, before the planner sees it. Approved, it is passed on
  byte-identical; rejected, the planner gets "Withheld by <gate>: <reason>" as the tool error
  instead (`check_handoff`, called by T2's DelegatingExecutor).
- **action**: each state-changing tool call, before it runs. Approved, it runs exactly as it
  would without the gate; rejected, the worker gets "Blocked by <gate>: <reason>" as the tool
  error (`GatedRuntime`).

So an approval changes nothing any agent sees. That keeps T3's and T4's requests identical to
T2's until the first rejection, which the cache sharing in PLAN_M2 §7 depends on.

BRIEF §4's interface is `on_tool_call(call, ctx) -> Allow | Block | AskHuman` and
`on_handoff(message, ctx) -> message`. Here both return a `Verdict`, and the checkpoint code
turns a rejection into the notice or error, so every gate rewrites messages the same way and a
gate can't alter an approved message. There is no AskHuman: the simulated human of T5 is a
gate of its own (lab/defenses/human.py). `on_untrusted_input` waits for M4.

Every verdict, approvals included, is traced (lab/tracing/events.py), so the analysis can say
which gate at which checkpoint caught what: a reviewer's or the policy's as a `review_verdict`,
the human's as an `approval_decision` answering the `approval_request` the human gate traced.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from agentdojo.functions_runtime import (
    FunctionCall,
    FunctionCallArgTypes,
    FunctionReturnType,
    FunctionsRuntime,
    TaskEnvironment,
)
from pydantic import ValidationError

from lab.models.types import RunContext
from lab.tracing.events import (
    TraceEvent,
    approval_decision_event,
    json_safe,
    review_verdict_event,
)

Checkpoint = Literal["handoff", "action"]
CHECKPOINTS: tuple[Checkpoint, ...] = ("handoff", "action")

# Banking's state-changing tools: the calls the action checkpoint sees (PLAN_M2 Q2). Reads
# change nothing, so they are never gated.
WRITE_TOOLS = frozenset({
    "send_money",
    "schedule_transaction",
    "update_scheduled_transaction",
    "update_password",
    "update_user_info",
})


@dataclass(frozen=True)
class HandoffCheck:
    """What a gate is shown at the handoff checkpoint. Never the worker's tool outputs."""

    user_request: str
    worker: str  # the worker's role, e.g. "account"
    instruction: str  # the planner's instruction to this worker
    report: str  # the worker's final message
    sources: tuple[int, ...]  # seqs of the events these texts were copied from (markers.py)


@dataclass(frozen=True)
class ToolCallCheck:
    """What a gate is shown at the action checkpoint: the raw call, before it runs."""

    user_request: str
    worker: str
    instruction: str
    call: FunctionCall
    sources: tuple[int, ...]
    agent_id: str  # the worker instance that made the call, e.g. "payments#1"
    runtime: FunctionsRuntime  # the worker's tools, for a gate that asks the worker (T5-summary)


@dataclass(frozen=True)
class Verdict:
    approve: bool
    reason: str
    agent_id: str  # who decided: "reviewer#k", "policy"
    agent_role: str
    call_idx: int | None = None  # the model call that produced it, if any
    output: str | None = None  # a model gate's reply, verbatim
    # The reply couldn't be read as a verdict, so the gate failed closed (reviewer_unparseable).
    unparseable: bool = False
    # Set by the human gate: the seq of the `approval_request` it traced. The verdict is then
    # traced as the `approval_decision` answering it, not as a `review_verdict`.
    approval_request: int | None = None
    reading: dict[str, Any] | None = None  # what T5-summary's parser read in the summary


class Gate(Protocol):
    name: str  # the component name in the presets table: "llm_reviewer", "policy_v1"
    label: str  # how the agents' notices name it: "the reviewer", "policy"
    checkpoints: frozenset[Checkpoint]

    def start(self, user_request: str, env: TaskEnvironment) -> None:
        """Called once per run, before any tool call, with the starting environment."""

    def on_handoff(self, check: HandoffCheck, ctx: RunContext) -> Verdict: ...

    def on_tool_call(self, check: ToolCallCheck, ctx: RunContext) -> Verdict: ...


def gates_at(gates: Sequence[Gate], checkpoint: Checkpoint) -> list[Gate]:
    return [g for g in gates if checkpoint in g.checkpoints]


@dataclass(frozen=True)
class Rejection:
    by: str  # the agent ID of the gate that rejected
    message: str  # what the agent gets instead: the withheld notice


def check_handoff(
    gates: Sequence[Gate],
    check: HandoffCheck,
    ctx: RunContext,
    *,
    planner: str,
    tool_call_id: str | None,
    result_seq: int,
) -> Rejection | None:
    """Put a worker's report through every handoff gate, in order, and trace each verdict.

    Returns the first rejection, or None if every gate approved (the report then goes to the
    planner unchanged). Later gates don't see a rejected report.
    """
    for gate in gates_at(gates, "handoff"):
        verdict = gate.on_handoff(check, ctx)
        delivered = check.report if verdict.approve else withheld_notice(gate, verdict)
        ctx.events.append(_verdict_event(
            ctx, gate, "handoff", verdict, planner, tool_call_id, None, check.sources,
            forwards=result_seq if verdict.approve else None, delivered=delivered,
        ))
        if not verdict.approve:
            return Rejection(by=verdict.agent_id, message=delivered)
    return None


def withheld_notice(gate: Gate, verdict: Verdict) -> str:
    return f"Withheld by {gate.label}: {verdict.reason}"


def blocked_error(gate: Gate, verdict: Verdict) -> str:
    return f"Blocked by {gate.label}: {verdict.reason}"


class GatedRuntime(FunctionsRuntime):
    """One worker's tools, with its write calls put through the action gates before they run.

    T2's DelegatingExecutor builds one per delegation, so it knows the worker, the planner's
    instruction and the user's request. A call that is not a write, names no known tool or has
    invalid arguments goes straight to FunctionsRuntime.run_function: it would not change
    anything, so there is nothing to gate, and the error it gets is AgentDojo's own.
    """

    def __init__(
        self,
        inner: FunctionsRuntime,
        gates: Sequence[Gate],
        ctx: RunContext,
        *,
        agent_id: str,
        worker: str,
        instruction: str,
        user_request: str,
        delegation_seq: int,
    ) -> None:
        super().__init__(list(inner.functions.values()))
        self.gates = gates_at(gates, "action")
        self.ctx = ctx
        self.agent_id = agent_id
        self.worker = worker
        self.instruction = instruction
        self.user_request = user_request
        self.delegation_seq = delegation_seq
        self._checked: set[tuple[int, int]] = set()  # (event seq, position) of gated calls

    def run_function(
        self,
        env: TaskEnvironment | None,
        function: str,
        kwargs: Mapping[str, FunctionCallArgTypes],
        raise_on_error: bool = False,
    ) -> tuple[FunctionReturnType, str | None]:
        if not self.gates or function not in WRITE_TOOLS or function not in self.functions:
            return super().run_function(env, function, kwargs, raise_on_error)
        try:
            self.functions[function].parameters.model_validate(dict(kwargs))
        except ValidationError:
            return super().run_function(env, function, kwargs, raise_on_error)

        call, call_seq = self._pending_call(function, kwargs)
        check = ToolCallCheck(
            user_request=self.user_request, worker=self.worker, instruction=self.instruction,
            call=call, sources=(self.delegation_seq, call_seq), agent_id=self.agent_id,
            runtime=self,
        )
        for gate in self.gates:
            verdict = gate.on_tool_call(check, self.ctx)
            error = None if verdict.approve else blocked_error(gate, verdict)
            if verdict.approval_request is not None:
                self.ctx.events.append(approval_decision_event(
                    len(self.ctx.events), verdict.agent_id, self.agent_id, gate=gate.name,
                    request=verdict.approval_request, approve=verdict.approve,
                    reason=verdict.reason, unparseable=verdict.unparseable,
                    tool_call_id=call.id, reading=verdict.reading, delivered=error,
                ))
            else:
                self.ctx.events.append(_verdict_event(
                    self.ctx, gate, "action", verdict, self.agent_id, call.id,
                    {"function": call.function, "args": json_safe(dict(call.args))},
                    check.sources, forwards=None, delivered=error,
                ))
            if error is not None:
                # The worker's LabLLM traces this result as the gate's, not the tools'.
                assert call.id is not None  # LabLLM gives every call an ID
                self.ctx.tool_result_senders[(self.agent_id, call.id)] = verdict.agent_id
                if raise_on_error:  # only nested calls ask for this; Banking has none
                    raise PermissionError(error)
                return "", error
        return super().run_function(env, function, kwargs, raise_on_error)

    def _pending_call(
        self, function: str, kwargs: Mapping[str, FunctionCallArgTypes]
    ) -> tuple[FunctionCall, int]:
        """The call being run, and the seq of the worker's message that made it.

        `run_function` isn't given the call's ID. The executor runs the calls of the worker's
        latest reply in order, so the call is the first one of that reply with this function
        and these arguments that hasn't been gated yet.
        """
        seq = max(
            e.seq for e in self.ctx.events
            if e.agent_id == self.agent_id and e.kind == "message"
            and e.payload["role"] == "assistant"
        )
        reply = self.ctx.transcripts[self.agent_id][-1]
        calls = (reply["tool_calls"] or []) if reply["role"] == "assistant" else []
        for i, call in enumerate(calls):
            if (seq, i) in self._checked:
                continue
            if call.function == function and dict(call.args) == dict(kwargs):
                self._checked.add((seq, i))
                return call, seq
        raise ValueError(f"{self.agent_id} runs {function}, but its last reply has no such call")


def _verdict_event(
    ctx: RunContext,
    gate: Gate,
    checkpoint: Checkpoint,
    verdict: Verdict,
    recipient: str,
    tool_call_id: str | None,
    call: dict[str, Any] | None,
    sources: tuple[int, ...],
    *,
    forwards: int | None,
    delivered: str | None,
) -> TraceEvent:
    return review_verdict_event(
        len(ctx.events), verdict.agent_id, verdict.agent_role, recipient, verdict.call_idx,
        gate=gate.name, checkpoint=checkpoint, approve=verdict.approve, reason=verdict.reason,
        unparseable=verdict.unparseable, output=verdict.output, tool_call_id=tool_call_id,
        call=call, sources=list(sources), forwards=forwards, delivered=delivered,
    )


def gate_decisions(events: Sequence[TraceEvent]) -> Counter[str]:
    """Verdicts per `<gate>@<checkpoint>:<verdict>`, plus the ones that failed closed because
    what the gate read couldn't be parsed: `reviewer_unparseable` (a reviewer's reply) and
    `summary_unparseable` (a worker's summary, T5-summary). Computed from the trace, as the
    report will (PLAN_M2 §8 `gate_decisions`). The human decides at the action checkpoint."""
    counts: Counter[str] = Counter()
    for e in events:
        p = e.payload
        if e.kind == "review_verdict":
            counts[f"{p['gate']}@{p['checkpoint']}:{p['verdict']}"] += 1
            if p["unparseable"]:
                counts["reviewer_unparseable"] += 1
        elif e.kind == "approval_decision":
            counts[f"{p['gate']}@action:{p['verdict']}"] += 1
            if p["unparseable"]:
                counts["summary_unparseable"] += 1
    return counts


def approvals_requested(events: Sequence[TraceEvent]) -> int:
    """How many times the run asked the human: the human's workload (PLAN_M2 Q4)."""
    return sum(e.kind == "approval_request" for e in events)
