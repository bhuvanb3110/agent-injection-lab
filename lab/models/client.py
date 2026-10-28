"""Thin OpenAI-compatible chat client (DECISIONS.md D1, D11).

One `chat.completions.create` call per `complete()`, nothing hidden:
- SDK retries are off (`max_retries=0`). The SDK default is 2 (openai/_constants.py), which
  would make calls our counters never see. Retrying is the scheduler's job.
- Temperature is always sent, including 0. Note that Groq converts 0 to 1e-8
  (https://console.groq.com/docs/openai).
- `tool_choice` is never sent: the OpenAI default with tools present is already "auto", and
  Ollama does not support the field (https://docs.ollama.com/api/openai-compatibility).
- Failures become typed errors from lab/errors.py. One 400 is not a failure: Groq's
  `tool_use_failed` is always the model's mistake in a tool call (an unoffered tool, broken JSON,
  arguments that fail the schema), and comes back as a response holding that call (D76, D82).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any, Protocol

import httpx2
import openai

from lab.errors import ConfigError, InfraError, ModelError
from lab.models.providers import ProviderConfig
from lab.models.types import ModelRequest, ModelResponse, RateLimitHeaders, RawToolCall
from lab.redact import redact

# Ollama requires a key but ignores it.
_NO_KEY_PLACEHOLDER = "unused"
DEFAULT_TIMEOUT_S = 60.0

# The statuses the openai SDK itself treats as transient (openai/_base_client.py:_should_retry).
_TRANSIENT_STATUSES = {408, 409, 429}
_CONFIG_STATUSES = {401, 403, 404}


class ChatClient(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...


class OpenAICompatClient:
    def __init__(
        self,
        provider: ProviderConfig,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        http_client: httpx2.Client | None = None,  # tests inject a mock transport here
        environ: Mapping[str, str] = os.environ,
    ) -> None:
        self.provider = provider
        self._sdk = openai.OpenAI(
            api_key=_read_api_key(provider, environ),
            base_url=provider.base_url,
            timeout=timeout_s,
            max_retries=0,
            http_client=http_client,
        )

    @property
    def max_retries(self) -> int:
        return self._sdk.max_retries

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.provider != self.provider.name:
            raise ConfigError(
                f"Request for provider {request.provider!r} sent to {self.provider.name!r} client"
            )
        model = self.provider.models.get(request.model_id)
        if model is None:
            raise ConfigError(
                f"Model {request.model_id!r} is not in configs/providers.yaml "
                f"for provider {self.provider.name!r}"
            )

        kwargs: dict[str, Any] = {
            "model": request.model_id,
            "messages": request.messages,
            "temperature": request.temperature,
        }
        if request.tools:
            kwargs["tools"] = request.tools
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.reasoning_effort is not None:
            kwargs["reasoning_effort"] = request.reasoning_effort
        if request.seed is not None:
            # Groq and Ollama both accept it; Groq calls it "best effort" (D27).
            kwargs["seed"] = request.seed
        if request.response_format is not None:
            # Only for models whose providers.yaml entry lists structured outputs (D62).
            kwargs["response_format"] = request.response_format

        start = time.perf_counter()
        try:
            # with_raw_response also gives the headers (openai/_legacy_response.py:
            # LegacyAPIResponse.headers); .parse() returns the same ChatCompletion as create().
            raw = self._sdk.chat.completions.with_raw_response.create(**kwargs)
            completion = raw.parse()
        except openai.APIConnectionError as e:  # includes APITimeoutError
            raise InfraError(
                self._redact(f"{type(e).__name__}: {e}"), provider=self.provider.name
            ) from e
        except openai.APIStatusError as e:
            attempt = recovered_call(e, request)
            if attempt is None:
                raise self._status_error(e) from e
            # Groq sends no usage with the error, so tokens and cost are unknown, never guessed.
            return ModelResponse(
                content=None,
                tool_calls=[attempt],
                finish_reason=TOOL_USE_FAILED,
                input_tokens=None,
                output_tokens=None,
                latency_s=time.perf_counter() - start,
                cost_usd=None,
                rate_limits=rate_limit_headers(e.response.headers),
            )
        latency_s = time.perf_counter() - start

        if not completion.choices:
            raise ModelError(
                "Response has no choices", provider=self.provider.name, subtype="no_choices"
            )
        choice = completion.choices[0]
        message = choice.message

        tool_calls = []
        for call in message.tool_calls or []:
            if call.type != "function":
                raise ModelError(
                    f"Unsupported tool call type {call.type!r}",
                    provider=self.provider.name,
                    subtype="unsupported_tool_call_type",
                )
            tool_calls.append(
                RawToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
            )

        usage = completion.usage
        input_tokens = usage.prompt_tokens if usage else None
        output_tokens = usage.completion_tokens if usage else None
        # Groq may leave out the reasoning count (D40): either no `completion_tokens_details`
        # or `reasoning_tokens: null`. Both give None here; the raw `usage` tells them apart.
        details = usage.completion_tokens_details if usage else None
        reasoning_tokens = details.reasoning_tokens if details else None
        cost_usd = (
            model.price.cost_usd(input_tokens, output_tokens)
            if input_tokens is not None and output_tokens is not None
            else None
        )
        reasoning = (message.model_extra or {}).get("reasoning")

        return ModelResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            reasoning=reasoning if isinstance(reasoning, str) else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            usage=usage.model_dump(mode="json", exclude_unset=True) if usage else None,
            latency_s=latency_s,
            cost_usd=cost_usd,
            rate_limits=rate_limit_headers(raw.headers),
        )

    def _status_error(self, e: openai.APIStatusError) -> Exception:
        status = e.status_code
        # The message comes from the provider's response body. Some providers echo part of the
        # key back on a 401, so it is redacted before it can reach a log or the store (D42).
        message = self._redact(f"HTTP {status} from {self.provider.name}: {e.message}")
        if status in _TRANSIENT_STATUSES or status >= 500:
            return InfraError(
                message,
                provider=self.provider.name,
                status_code=status,
                retry_after_s=_retry_after_s(e.response),
            )
        if status in _CONFIG_STATUSES:
            return ConfigError(message, status_code=status)
        return ModelError(
            message, provider=self.provider.name, subtype=f"http_{status}", status_code=status
        )

    def _redact(self, text: str) -> str:
        # The SDK holds the key; it is used here only to remove it, never stored or printed.
        return redact(text, secrets=[self._sdk.api_key])


TOOL_USE_FAILED = "tool_use_failed"
_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')
_ARGUMENTS_TO_END = re.compile(r'"arguments"\s*:\s*(.*)\}\s*$', re.DOTALL)
_JSON_PARSE_FAILURE = "Failed to parse tool call arguments as JSON"
UNKNOWN_TOOL = "unknown_tool"


def recovered_call(error: openai.APIStatusError, request: ModelRequest) -> RawToolCall | None:
    """The tool call in a Groq 400 `tool_use_failed` body, whatever was wrong with it (D76,
    D82); None only if the error is not that, and then it stands as before.

    Groq validates tool calls server-side and rejects the whole response when one is bad; the
    model's attempt is in `failed_generation`. Every such rejection is the model's mistake, so it
    is classified from Groq's `message` and `failed_generation`, in this order:
    - a JSON object {"name", "arguments"} naming a tool not in `request.tools`: out of scope;
    - the same naming an offered tool: invalid arguments (they fail its schema);
    - Groq's "Failed to parse tool call arguments as JSON" with a readable name: malformed,
      with the model's raw argument text;
    - anything else, or no `failed_generation`: unparsed, keeping the whole text.
    The SDK puts the body's `error` object in `error.body` (openai/_client.py:
    _make_status_error). Groq's docs don't describe these fields; the shapes come from the
    bodies seen in m2_exit and m2_exit_v2 (stored in the `requests` table).
    """
    body = error.body
    if error.status_code != 400 or not isinstance(body, dict):
        return None
    if body.get("code") != TOOL_USE_FAILED:
        return None
    generation = body.get("failed_generation")
    generation = generation if isinstance(generation, str) else ""
    message = body.get("message")
    message = message if isinstance(message, str) else ""
    # Groq gives the attempt no ID. One derived from the request and the attempt is stable, and
    # unique within a transcript, since each request of an agent holds a longer history.
    digest = hashlib.sha256((request.model_dump_json() + generation).encode("utf-8")).hexdigest()

    def call(prefix: str, name: str, arguments: str, **fields: Any) -> RawToolCall:
        return RawToolCall(
            id=f"{prefix}_{digest[:24]}", name=name, arguments=arguments, **fields
        )

    try:
        value = json.loads(generation)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict) and set(value) == {"name", "arguments"}:
        name, arguments = value["name"], value["arguments"]
        if isinstance(name, str) and isinstance(arguments, dict):
            if name not in {tool["function"]["name"] for tool in request.tools}:
                return call("oos", name, json.dumps(arguments), out_of_scope=True)
            # Groq's message ends in the problem: "... did not match schema: errors: [...]".
            problem = message.split("validation failed: ")[-1]
            return call(
                "rej", name, json.dumps(arguments),
                rejection="invalid_arguments", detail=problem,
            )
    named = _NAME.search(generation)
    name = named.group(1) if named else UNKNOWN_TOOL
    if _JSON_PARSE_FAILURE in message and named:
        text = _ARGUMENTS_TO_END.search(generation)
        raw = text.group(1).strip() if text else ""
        if raw and not _is_json_object(raw):
            return call("rej", name, raw, rejection="malformed", detail=message)
    return call("rej", name, generation, rejection="unparsed", detail=message)


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except json.JSONDecodeError:
        return False


def _read_api_key(provider: ProviderConfig, environ: Mapping[str, str]) -> str:
    if provider.api_key_env is None:
        return _NO_KEY_PLACEHOLDER
    key = environ.get(provider.api_key_env)
    if not key:
        # Name the variable, never the value.
        raise ConfigError(f"{provider.api_key_env} is not set (see .env.example)")
    return key


def _retry_after_s(response: httpx2.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None  # HTTP-date form; the scheduler falls back to its own backoff


def rate_limit_headers(headers: Mapping[str, str]) -> RateLimitHeaders | None:
    """Parse Groq's x-ratelimit-* headers; None if the provider sent none of them (Ollama)."""
    parsed = RateLimitHeaders(
        remaining_requests_per_day=_int_or_none(headers.get("x-ratelimit-remaining-requests")),
        requests_reset_s=parse_duration_s(headers.get("x-ratelimit-reset-requests")),
        remaining_tokens_per_minute=_int_or_none(headers.get("x-ratelimit-remaining-tokens")),
        tokens_reset_s=parse_duration_s(headers.get("x-ratelimit-reset-tokens")),
    )
    return None if parsed == RateLimitHeaders() else parsed


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_S = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def parse_duration_s(value: str | None) -> float | None:
    """Parse durations like "1m26.4s", "3.165s" or "250ms" into seconds; None if unparseable."""
    if not value:
        return None
    parts = _DURATION_PART.findall(value)
    if "".join(number + unit for number, unit in parts) != value.strip():
        return None
    return sum(float(number) * _UNIT_S[unit] for number, unit in parts)


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
