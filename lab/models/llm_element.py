"""AgentDojo pipeline element backed by our client, plus the tool executor that goes with it.

`LabLLM` replaces AgentDojo's `OpenAILLM` (agentdojo/agent_pipeline/llms/openai_llm.py) and
differs on purpose (DECISIONS.md D1, D14, D15):
- the system prompt is sent with role "system", not "developer";
- content is sent as plain strings, and tool messages carry no `name` field, which Groq
  rejects with a 400 (https://console.groq.com/docs/openai);
- tool-call arguments that aren't a JSON object don't crash the run. The call becomes a
  `MalformedToolCall`, `LabToolsExecutor` answers it with a tool error the model can recover
  from, and the run's count goes up (D6).

Every call appends a `CallRecord` to the `RunContext` in `extra_args["ctx"]` (D17), and a
response cut off by max_tokens (finish_reason "length") ends the run as ModelError "truncated"
(D29).
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


class MalformedToolCall(FunctionCall):
    """A tool call whose arguments could not be parsed. It is never executed.

    `args` is empty; the model's original text is kept in `raw_arguments` for the trace.
    """

    raw_arguments: str
    parse_error: str


# FunctionCall.args uses the forward reference "FunctionCallArgTypes", which pydantic resolves
# in the subclass's module, so make it visible here and finish building the model.
MalformedToolCall.model_rebuild(_types_namespace={"FunctionCallArgTypes": FunctionCallArgTypes})


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
    ) -> None:
        self.client = client
        self.provider = provider
        self.model_id = model_id
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.seed = seed
        self.name = f"{provider}/{model_id}"

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

        request = ModelRequest(
            provider=self.provider,
            model_id=self.model_id,
            messages=[message_to_openai(m) for m in messages],
            tools=[function_to_openai(f) for f in runtime.functions.values()],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
            seed=self.seed,
            repeat_idx=ctx.repeat_idx,
        )
        # Keep the transcript in the context, so a run that ends in ModelError still has one.
        ctx.messages = list(messages)
        response = self.client.complete(request)
        assistant = response_to_assistant_message(response)
        malformed = sum(isinstance(c, MalformedToolCall) for c in assistant["tool_calls"] or [])

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
                latency_s=response.latency_s,
                cost_usd=response.cost_usd,
                finish_reason=response.finish_reason,
                malformed_tool_calls=malformed,
                reasoning=response.reasoning,
            )
        )
        ctx.messages = [*messages, assistant]
        if response.finish_reason == "length":
            # Cut off by max_tokens: the answer or tool call may be incomplete, so the run ends
            # here as a model error rather than acting on half a message (D29). The call above
            # is already counted and the partial message is in ctx.messages.
            raise ModelError(
                f"{self.name}: response cut off at max_tokens={self.max_tokens}",
                provider=self.provider,
                subtype="truncated",
            )
        return query, runtime, env, ctx.messages, extra_args


class LabToolsExecutor(ToolsExecutor):
    """AgentDojo's ToolsExecutor, except that malformed calls get a tool error, not a run.

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
        if not any(isinstance(c, MalformedToolCall) for c in last["tool_calls"]):
            return super().query(query, runtime, env, messages, extra_args)

        results: list[ChatMessage] = []
        for call in last["tool_calls"]:
            if isinstance(call, MalformedToolCall):
                results.append(_malformed_call_result(call))
                continue
            single = ChatAssistantMessage(role="assistant", content=None, tool_calls=[call])
            query, runtime, env, out, extra_args = super().query(
                query, runtime, env, [single], extra_args
            )
            results.extend(out[1:])
        return query, runtime, env, [*messages, *results], extra_args


def _malformed_call_result(call: MalformedToolCall) -> ChatToolResultMessage:
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
        if args is None:
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
