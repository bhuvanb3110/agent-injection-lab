"""AgentDojo pipeline element backed by our client, plus the tool executor that goes with it.

`LabLLM` replaces AgentDojo's `OpenAILLM` (agentdojo/agent_pipeline/llms/openai_llm.py) and
differs on purpose (DECISIONS.md D1, D14, D15):
- the system prompt is sent with role "system", not "developer";
- content is sent as plain strings, and tool messages carry no `name` field, which Groq
  rejects with a 400 (https://console.groq.com/docs/openai);
- tool-call arguments that aren't a JSON object don't crash the run. The call becomes a
  `MalformedToolCall`, `LabToolsExecutor` answers it with a tool error the model can recover
  from, and the run's count goes up (D6);
- a call to a tool the agent doesn't have (an `OutOfScopeToolCall`, D76) is never run either: the
  agent gets the tool error "Tool X is not available to you." and the run goes on. An agent's
  fourth such call in a run ends the run as ModelError "out_of_scope_tool_calls".

Every call appends a `CallRecord` to the `RunContext` in `extra_args["ctx"]` (D17), keeps the
agent's transcript there (D30), and adds a trace event for every message the agent was sent or
wrote (lab/tracing/events.py). A response cut off by max_tokens (finish_reason "length") ends
the run as ModelError "truncated" (D29).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.tool_execution import ToolsExecutor
from agentdojo.functions_runtime import (
    EmptyEnv,
    Env,
    Function,
    FunctionCall,
    FunctionCallArgTypes,
    FunctionsRuntime,
)
from agentdojo.types import (
    ChatAssistantMessage,
    ChatMessage,
    ChatToolResultMessage,
    MessageContentBlock,
    text_content_block_from_string,
)

from lab.errors import ModelError
from lab.models.cache import request_key
from lab.models.client import ChatClient
from lab.models.providers import ReasoningEffort
from lab.models.types import CallRecord, ModelRequest, ModelResponse, RunContext
from lab.scheduler.limits import estimate_tokens
from lab.tracing.events import SINGLE_AGENT, SINGLE_ROLE, TOOLS, USER, TraceEvent, message_event


class MalformedToolCall(FunctionCall):
    """A tool call whose arguments could not be parsed. It is never executed.

    `args` is empty; the model's original text is kept in `raw_arguments` for the trace.
    """

    raw_arguments: str
    parse_error: str


# FunctionCall.args uses the forward reference "FunctionCallArgTypes", which pydantic resolves
# in the subclass's module, so make it visible here and finish building the model.
MalformedToolCall.model_rebuild(_types_namespace={"FunctionCallArgTypes": FunctionCallArgTypes})


class OutOfScopeToolCall(FunctionCall):
    """A call to a tool the agent wasn't offered (D76). It is never executed.

    `out_of_scope` is a field, not only the class, so the trace payload says what the call was:
    the markers search its arguments as `tool_call_args` whoever the agent sends calls to.
    """

    out_of_scope: bool = True


OutOfScopeToolCall.model_rebuild(_types_namespace={"FunctionCallArgTypes": FunctionCallArgTypes})

# An agent's out-of-scope calls allowed per run; the next one ends the run as a model error
# (D76), so a model that keeps trying can't use up every round.
MAX_OUT_OF_SCOPE_CALLS = 3


class LabLLM(BasePipelineElement):
    def __init__(
        self,
        client: ChatClient,
        *,
        provider: str,
        model_id: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        seed: int | None = None,
        agent_id: str = SINGLE_AGENT,
        agent_role: str = SINGLE_ROLE,
        principal: str = USER,
        tool_target: str = TOOLS,
        structured_outputs: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.provider = provider
        self.model_id = model_id
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.seed = seed
        # Who this model is in the run: names its transcript, its calls and its trace events.
        self.agent_id = agent_id
        self.agent_role = agent_role
        # The edges of its trace events: whom it answers to, and where its tool calls go.
        self.principal = principal
        self.tool_target = tool_target
        # Whether the model accepts a json_schema `response_format` (providers.yaml, D62), and
        # the one this agent sends with every call (None: none is sent).
        self.structured_outputs = structured_outputs
        self.response_format = response_format
        self.name = f"{provider}/{model_id}"

    def as_agent(
        self,
        agent_id: str,
        agent_role: str,
        *,
        principal: str = USER,
        tool_target: str = TOOLS,
        response_format: dict[str, Any] | None = None,
    ) -> LabLLM:
        """The same model, client and sampling, playing another agent of the run (T2+)."""
        return LabLLM(
            self.client,
            provider=self.provider,
            model_id=self.model_id,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
            seed=self.seed,
            agent_id=agent_id,
            agent_role=agent_role,
            principal=principal,
            tool_target=tool_target,
            structured_outputs=self.structured_outputs,
            response_format=response_format,
        )

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        # Defaults copied from BasePipelineElement.query; AgentDojo's own typing of `env`
        # doesn't pass strict mypy.
        env: Env = EmptyEnv(),  # type: ignore[assignment]  # noqa: B008
        messages: Sequence[ChatMessage] = (),
        extra_args: dict[str, Any] = {},  # noqa: B006
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict[str, Any]]:
        ctx = extra_args.get("ctx")
        if not isinstance(ctx, RunContext):
            # Without a context the call would not be counted, so refuse to make it.
            raise ValueError("LabLLM needs a RunContext in extra_args['ctx']")

        request = self._request(messages, runtime, ctx)
        # Keep the transcript and the trace in the context, so a run that ends in ModelError
        # still has both, up to and including what this call was sent.
        self._record_new_messages(ctx, messages)
        ctx.transcripts[self.agent_id] = list(messages)
        assistant = self._call(request, ctx)
        transcript = [*messages, assistant]
        ctx.transcripts[self.agent_id] = transcript
        ctx.events.append(self._event(len(ctx.events), assistant, len(ctx.calls) - 1))
        self._raise_if_truncated(ctx)
        self._raise_if_out_of_scope_cap(ctx)
        return query, runtime, env, transcript, extra_args

    def side_call(
        self, messages: Sequence[ChatMessage], runtime: FunctionsRuntime, ctx: RunContext
    ) -> tuple[ChatAssistantMessage, int]:
        """One call made for a gate on this agent's behalf, e.g. T5-summary's description of a
        pending action (D65). It is counted in `ctx.calls` like any call, but nothing is added
        to the agent's transcript or the trace: the gate traces the reply itself. Returns the
        reply and its row in `calls`."""
        assistant = self._call(self._request(messages, runtime, ctx), ctx)
        self._raise_if_truncated(ctx)
        return assistant, len(ctx.calls) - 1

    def _request(
        self, messages: Sequence[ChatMessage], runtime: FunctionsRuntime, ctx: RunContext
    ) -> ModelRequest:
        """The request for these messages. Its `occurrence` counts the identical requests the
        run has made before it, so a repeat within the run is a new sample (D68)."""
        request = ModelRequest(
            provider=self.provider,
            model_id=self.model_id,
            messages=[message_to_openai(m) for m in messages],
            tools=[function_to_openai(f) for f in runtime.functions.values()],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
            seed=self.seed,
            response_format=self.response_format,
            repeat_idx=ctx.repeat_idx,
        )
        first = request_key(request)
        occurrence = ctx.request_counts.get(first, 0)
        ctx.request_counts[first] = occurrence + 1
        return request.model_copy(update={"occurrence": occurrence}) if occurrence else request

    def _call(self, request: ModelRequest, ctx: RunContext) -> ChatAssistantMessage:
        """Send the request and append its CallRecord to the context."""
        response = self.client.complete(request)
        assistant = response_to_assistant_message(response)
        calls = assistant["tool_calls"] or []
        malformed = sum(isinstance(c, MalformedToolCall) for c in calls)
        out_of_scope = sum(isinstance(c, OutOfScopeToolCall) for c in calls)
        estimate = None
        if response.input_tokens is None or response.output_tokens is None:
            estimate = estimate_tokens(request)
        ctx.calls.append(
            CallRecord(
                provider=self.provider,
                model_id=self.model_id,
                request_key=request_key(request),
                cached=response.cached,
                attempts=response.attempts,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                reasoning_tokens=response.reasoning_tokens,
                usage=response.usage,
                latency_s=response.latency_s,
                cost_usd=response.cost_usd,
                finish_reason=response.finish_reason,
                malformed_tool_calls=malformed,
                out_of_scope_tool_calls=out_of_scope,
                estimated_input_tokens=estimate.input_tokens if estimate else None,
                estimated_output_tokens=estimate.output_tokens if estimate else None,
                reasoning=response.reasoning,
                agent_id=self.agent_id,
            )
        )
        return assistant

    def _raise_if_truncated(self, ctx: RunContext) -> None:
        if ctx.calls[-1].finish_reason == "length":
            # Cut off by max_tokens: the answer or tool call may be incomplete, so the run ends
            # here as a model error rather than acting on half a message (D29). The call is
            # already counted and the partial message is in the transcript and the trace.
            raise ModelError(
                f"{self.name}: response cut off at max_tokens={self.max_tokens}",
                provider=self.provider,
                subtype="truncated",
            )

    def _raise_if_out_of_scope_cap(self, ctx: RunContext) -> None:
        made = ctx.out_of_scope_by(self.agent_id)
        if made > MAX_OUT_OF_SCOPE_CALLS:
            # Like truncation, the call is counted and in the transcript and the trace first,
            # so its arguments are still searched for markers.
            raise ModelError(
                f"{self.name}: agent {self.agent_id} made {made} calls to tools it doesn't have "
                f"(the limit is {MAX_OUT_OF_SCOPE_CALLS} per run)",
                provider=self.provider,
                subtype="out_of_scope_tool_calls",
            )

    def _record_new_messages(self, ctx: RunContext, messages: Sequence[ChatMessage]) -> None:
        """Add a trace event for each message this agent hasn't been sent before: the system
        prompt and request on the first call, then the tool results since its last reply."""
        seen = ctx.transcripts.get(self.agent_id, [])
        if list(messages[: len(seen)]) != seen:
            # The trace assumes transcripts only grow. A pipeline that rewrites one is a bug.
            raise ValueError(f"The transcript of agent {self.agent_id!r} was rewritten")
        for message in messages[len(seen) :]:
            sender = None
            if message["role"] == "tool" and message["tool_call_id"] is not None:
                # A result a gate wrote in place of the tools' (a blocked call).
                sender = ctx.tool_result_senders.get((self.agent_id, message["tool_call_id"]))
            ctx.events.append(self._event(len(ctx.events), message, None, sender=sender))

    def _event(
        self, seq: int, message: ChatMessage, call_idx: int | None, *, sender: str | None = None
    ) -> TraceEvent:
        return message_event(
            seq, self.agent_id, self.agent_role, message, call_idx,
            principal=self.principal, tool_target=self.tool_target, sender=sender,
        )


class LabToolsExecutor(ToolsExecutor):
    """AgentDojo's ToolsExecutor, except that malformed and out-of-scope calls get a tool error,
    not a run.

    Valid calls are handed one at a time to the parent class, so they execute exactly as in
    AgentDojo. Results stay in the order the model made the calls. Any topology that uses
    `LabLLM` must use this executor: the stock one would run a malformed call with empty args.
    """

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        # Defaults copied from BasePipelineElement.query; AgentDojo's own typing of `env`
        # doesn't pass strict mypy.
        env: Env = EmptyEnv(),  # type: ignore[assignment]  # noqa: B008
        messages: Sequence[ChatMessage] = (),
        extra_args: dict[str, Any] = {},  # noqa: B006
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict[str, Any]]:
        last = messages[-1] if messages else None
        if last is None or last["role"] != "assistant" or not last["tool_calls"]:
            return super().query(query, runtime, env, messages, extra_args)
        if not any(not_run_result(c) for c in last["tool_calls"]):
            return super().query(query, runtime, env, messages, extra_args)

        results: list[ChatMessage] = []
        for call in last["tool_calls"]:
            refused = not_run_result(call)
            if refused is not None:
                results.append(refused)
                continue
            single = ChatAssistantMessage(role="assistant", content=None, tool_calls=[call])
            query, runtime, env, out, extra_args = super().query(
                query, runtime, env, [single], extra_args
            )
            results.extend(out[1:])
        return query, runtime, env, [*messages, *results], extra_args


def not_run_result(call: FunctionCall) -> ChatToolResultMessage | None:
    """The tool error for a call that is never run (malformed, D15; out of scope, D76), or None
    for a call that may run. Every executor that takes a LabLLM's calls asks this first."""
    if isinstance(call, MalformedToolCall):
        return malformed_call_result(call)
    if isinstance(call, OutOfScopeToolCall):
        return ChatToolResultMessage(
            role="tool",
            content=[text_content_block_from_string("")],
            tool_call_id=call.id,
            tool_call=call,
            error=f"Tool {call.function} is not available to you.",
        )
    return None


def malformed_call_result(call: MalformedToolCall) -> ChatToolResultMessage:
    return ChatToolResultMessage(
        role="tool",
        content=[text_content_block_from_string("")],
        tool_call_id=call.id,
        tool_call=call,
        error=(
            f"Malformed tool call: the arguments for `{call.function}` are not a valid JSON "
            f"object ({call.parse_error}). The tool was not run. Call it again with valid JSON "
            f"arguments. Arguments received: {call.raw_arguments}"
        ),
    )


# --- AgentDojo messages -> OpenAI format ---------------------------------------------------


def _text(content: list[MessageContentBlock] | None) -> str | None:
    # Text blocks only: thinking blocks are not something the model should be shown as text.
    if content is None:
        return None
    return "\n".join(block["content"] for block in content if block["type"] == "text")


def _tool_call_to_openai(call: FunctionCall) -> dict[str, Any]:
    if call.id is None:
        raise ValueError(f"Tool call {call.function!r} has no id")
    # A malformed call is replayed with "{}": the provider may reject invalid JSON in the
    # history, and the model already sees its original text in the tool error.
    arguments = "{}" if isinstance(call, MalformedToolCall) else json.dumps(dict(call.args))
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.function, "arguments": arguments},
    }


def message_to_openai(message: ChatMessage) -> dict[str, Any]:
    match message["role"]:
        case "system":
            return {"role": "system", "content": _text(message["content"]) or ""}
        case "user":
            return {"role": "user", "content": _text(message["content"]) or ""}
        case "assistant":
            out: dict[str, Any] = {"role": "assistant", "content": _text(message["content"])}
            if message["tool_calls"]:
                out["tool_calls"] = [_tool_call_to_openai(c) for c in message["tool_calls"]]
            return out
        case "tool":
            if message["tool_call_id"] is None:
                raise ValueError("Tool result has no tool_call_id")
            # Same rule as AgentDojo's OpenAILLM: an error replaces the (empty) result.
            content = message["error"] or _text(message["content"]) or ""
            return {"role": "tool", "tool_call_id": message["tool_call_id"], "content": content}
    raise ValueError(f"Unknown message role: {message['role']!r}")


def function_to_openai(function: Function) -> dict[str, Any]:  # type: ignore[type-arg]
    return {
        "type": "function",
        "function": {
            "name": function.name,
            "description": function.description,
            "parameters": function.parameters.model_json_schema(),
        },
    }


# --- OpenAI format -> AgentDojo messages ---------------------------------------------------


def _parse_arguments(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return (args, None) for a JSON object, else (None, reason). Nothing else is accepted."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    if not isinstance(value, dict):
        return None, f"expected a JSON object, got {type(value).__name__}"
    return value, None


def response_to_assistant_message(response: ModelResponse) -> ChatAssistantMessage:
    tool_calls: list[FunctionCall] = []
    for raw in response.tool_calls:
        args, error = _parse_arguments(raw.arguments)
        if raw.out_of_scope:
            if args is None:
                # The client only builds these from a JSON object (client.py:out_of_scope_call).
                raise ValueError(f"Out-of-scope call {raw.name!r} has arguments that aren't one")
            tool_calls.append(OutOfScopeToolCall(function=raw.name, args=args, id=raw.id))
        elif args is None:
            tool_calls.append(
                MalformedToolCall(
                    function=raw.name,
                    args={},
                    id=raw.id,
                    raw_arguments=raw.arguments,
                    parse_error=error or "",
                )
            )
        else:
            tool_calls.append(FunctionCall(function=raw.name, args=args, id=raw.id))

    content: list[MessageContentBlock] | None = (
        None if response.content is None else [text_content_block_from_string(response.content)]
    )
    return ChatAssistantMessage(role="assistant", content=content, tool_calls=tool_calls or None)
