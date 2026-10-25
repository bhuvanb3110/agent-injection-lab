"""Data passed between the LLM element, the cache and the clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict


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
    # Not sent. Separates repeats in the cache so repeat 2 is a fresh sample, not a replay of
    # repeat 1, which would hide run-to-run variance (D13).
    repeat_idx: int = 0


class RawToolCall(BaseModel):
    """A tool call exactly as the model emitted it; `arguments` is unparsed JSON text."""

    id: str
    name: str
    arguments: str


class ModelResponse(BaseModel):
    content: str | None
    tool_calls: list[RawToolCall]
    finish_reason: str | None
    # Reasoning text from reasoning models (Groq's `reasoning` field). Kept for traces only,
    # never put into the transcript, where it would leak into the scored answer (D14).
    reasoning: str | None = None
    # None when the provider reported no usage; never guessed.
    input_tokens: int | None
    output_tokens: int | None
    latency_s: float
    cost_usd: float | None
    cached: bool = False


class CallRecord(BaseModel):
    """One model call as the run store will see it."""

    provider: str
    model_id: str
    request_key: str  # hash of the ModelRequest; the cache key, also used to link traces
    cached: bool
    input_tokens: int | None
    output_tokens: int | None
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

    @property
    def malformed_tool_calls(self) -> int:
        return sum(call.malformed_tool_calls for call in self.calls)
