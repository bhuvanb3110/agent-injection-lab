"""Thin OpenAI-compatible chat client (DECISIONS.md D1, D11).

One `chat.completions.create` call per `complete()`, nothing hidden:
- SDK retries are off (`max_retries=0`). The SDK default is 2 (openai/_constants.py), which
  would make calls our counters never see. Retrying is the scheduler's job.
- Temperature is always sent, including 0. Note that Groq converts 0 to 1e-8
  (https://console.groq.com/docs/openai).
- `tool_choice` is never sent: the OpenAI default with tools present is already "auto", and
  Ollama does not support the field (https://docs.ollama.com/api/openai-compatibility).
- Failures become typed errors from lab/errors.py.
"""

from __future__ import annotations

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

        start = time.perf_counter()
        try:
            # with_raw_response also gives the headers (openai/_legacy_response.py:
            # LegacyAPIResponse.headers); .parse() returns the same ChatCompletion as create().
            raw = self._sdk.chat.completions.with_raw_response.create(**kwargs)
            completion = raw.parse()
        except openai.APIConnectionError as e:  # includes APITimeoutError
            raise InfraError(f"{type(e).__name__}: {e}", provider=self.provider.name) from e
        except openai.APIStatusError as e:
            raise self._status_error(e) from e
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
            latency_s=latency_s,
            cost_usd=cost_usd,
            rate_limits=rate_limit_headers(raw.headers),
        )

    def _status_error(self, e: openai.APIStatusError) -> Exception:
        status = e.status_code
        # The error message comes from the provider's response body, never from our request,
        # so it cannot contain the API key.
        message = f"HTTP {status} from {self.provider.name}: {e.message}"
        if status in _TRANSIENT_STATUSES or status >= 500:
            return InfraError(
                message,
                provider=self.provider.name,
                status_code=status,
                retry_after_s=_retry_after_s(e.response),
            )
        if status in _CONFIG_STATUSES:
            return ConfigError(message)
        return ModelError(
            message, provider=self.provider.name, subtype=f"http_{status}", status_code=status
        )


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
