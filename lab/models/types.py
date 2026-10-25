"""Data passed between the LLM element, the cache and the clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentdojo.types import ChatMessage
from pydantic import BaseModel, ConfigDict, Field

from lab.models.providers import ReasoningEffort


class ModelRequest(BaseModel):
    """Everything sent to the provider (plus the repeat index), and nothing secret.

    The cache key is a hash of this object (lab/models/cache.py), so it must hold every field
    that can change the response, and it must never hold the API key, which lives only inside
    the HTTP client (DECISIONS.md D12).
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model_id: str
    messages: list[dict[str, Any]]  # OpenAI chat format
    tools: list[dict[str, Any]]  # OpenAI tool format; empty means none are sent
    temperature: float
    max_tokens: int | None = None  # None: not sent, the provider default applies
    reasoning_effort: ReasoningEffort | None = None  # None: not sent (D19)
    seed: int | None = None  # None: not sent, the provider picks its own (D27)
    # Not sent. Separates repeats in the cache so repeat 2 is a fresh sample, not a replay of
    # repeat 1, which would hide run-to-run variance (D13).
    repeat_idx: int = 0


class RawToolCall(BaseModel):
    """A tool call exactly as the model emitted it; `arguments` is unparsed JSON text."""

    id: str
    name: str
    arguments: str


class RateLimitHeaders(BaseModel):
    """Groq's x-ratelimit-* headers (https://console.groq.com/docs/rate-limits, D20).

    The request headers track requests per *day*; the token headers track tokens per *minute*.
    Any field the provider didn't send is None.
    """

    remaining_requests_per_day: int | None = None
    requests_reset_s: float | None = None
    remaining_tokens_per_minute: int | None = None
    tokens_reset_s: float | None = None


class ModelResponse(BaseModel):
    content: str | None
    tool_calls: list[RawToolCall]
    finish_reason: str | None
    # Reasoning text from reasoning models (Groq's `reasoning` field). Kept for traces only,
    # never put into the transcript, where it would leak into the scored answer (D14).
    reasoning: str | None = None
    # None when the provider reported no usage; never guessed. `output_tokens` already includes
    # the reasoning tokens; `reasoning_tokens` is the part of it spent on reasoning (D19).
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None = None
    # The provider's `usage` object exactly as sent (only the keys it set), so a missing count
    # can be traced to what the provider returned (D40). None if it sent no usage.
    usage: dict[str, Any] | None = None
    latency_s: float
    cost_usd: float | None
    cached: bool = False
    # For the scheduler only: describes the provider's state when the call was made, not the
    # response, so it is excluded from the cache.
    rate_limits: RateLimitHeaders | None = Field(default=None, exclude=True)
    # Real requests it took: 1 plus retries, 0 for a cache hit. Set by ScheduledClient and
    # CachingClient; not cached, for the same reason.
    attempts: int = Field(default=1, exclude=True)


class CallRecord(BaseModel):
    """One model call as the run store will see it."""

    provider: str
    model_id: str
    request_key: str  # hash of the ModelRequest; the cache key, also used to link traces
    cached: bool
    attempts: int = 1  # real requests behind this call: retries included, 0 for a cache hit
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None = None
    usage: dict[str, Any] | None = None  # raw provider usage (D40)
    latency_s: float
    cost_usd: float | None
    finish_reason: str | None
    malformed_tool_calls: int
    reasoning: str | None = None


@dataclass
class RunContext:
    """Per-run accumulator, passed through the pipeline in `extra_args["ctx"]` (D17)."""

    repeat_idx: int = 0
    calls: list[CallRecord] = field(default_factory=list)
    # The transcript as of the latest model call, kept up to date by LabLLM. When a run ends in
    # ModelError the pipeline raises before returning its messages; this is what gets stored
    # instead (D30).
    messages: list[ChatMessage] = field(default_factory=list)

    @property
    def malformed_tool_calls(self) -> int:
        return sum(call.malformed_tool_calls for call in self.calls)
