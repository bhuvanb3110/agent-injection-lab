"""Data passed between the LLM element, the cache and the clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentdojo.types import ChatMessage
from pydantic import BaseModel, ConfigDict, Field

from lab.models.providers import ReasoningEffort
from lab.tracing.events import SINGLE_AGENT, TraceEvent
from lab.tracing.markers import Marker


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
    # OpenAI's `response_format`, e.g. a strict json_schema for the reviewer's verdict (D62).
    # None: not sent, and left out of the cache key, so no earlier key changes.
    response_format: dict[str, Any] | None = None
    # Not sent. Separates repeats in the cache so repeat 2 is a fresh sample, not a replay of
    # repeat 1, which would hide run-to-run variance (D13).
    repeat_idx: int = 0
    # Not sent. How many identical requests this run made before this one (D68). A repeated
    # request within a run (two workers given the same instruction) is then a new sample, not a
    # cache replay. 0, the first occurrence, is left out of the cache key, so no earlier key
    # changes; and since it counts within the run, a child topology that repeats its parent's
    # requests repeats their indices too, and still shares the parent's cache.
    occurrence: int = 0


class RawToolCall(BaseModel):
    """A tool call exactly as the model emitted it; `arguments` is unparsed JSON text."""

    id: str
    name: str
    arguments: str
    # A call to a tool the request didn't offer, which the provider refused to return as a tool
    # call (Groq's 400 `tool_use_failed`) and the client recovered from the error body (D76).
    # It is never run. False in every response cached before D76.
    out_of_scope: bool = False
    # Another call Groq's 400 `tool_use_failed` refused and the client recovered (D82), and why:
    # "malformed" (arguments not JSON; `arguments` is the model's raw text), "invalid_arguments"
    # (JSON that fails the tool's schema) or "unparsed" (the generation could not be read at
    # all). `detail` is Groq's message. Never run. None in every response cached before D82.
    rejection: str | None = None
    detail: str | None = None


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
    # Calls to a tool the agent doesn't have (D76). 0 in calls stored before D76.
    out_of_scope_tool_calls: int = 0
    # Calls Groq rejected as `tool_use_failed` that the client recovered (D82), by kind. The
    # malformed ones are also in `malformed_tool_calls`. `rejected_tool_calls` is all four kinds
    # together (out of scope included): what the per-agent cap counts.
    invalid_argument_tool_calls: int = 0
    unparsed_tool_calls: int = 0
    rejected_tool_calls: int = 0
    # The limiter's pre-send estimate (lab/scheduler/limits.py:estimate_tokens), kept only for a
    # call the provider sent no usage for (D79). An estimate, not a measurement: the input is
    # characters / CHARS_PER_TOKEN, the output the reserve. None when usage is known, and in
    # calls stored before D79.
    estimated_input_tokens: int | None = None
    estimated_output_tokens: int | None = None
    reasoning: str | None = None
    # The agent that made the call. Not part of the request, so not part of the cache key (D12).
    # None in calls stored before milestone 2.
    agent_id: str | None = None


@dataclass
class RunContext:
    """Per-run accumulator, passed through the pipeline in `extra_args["ctx"]` (D17)."""

    repeat_idx: int = 0
    calls: list[CallRecord] = field(default_factory=list)
    # Each agent's transcript as of its latest model call, and every message of every agent as
    # a trace event, kept up to date by LabLLM. When a run ends in ModelError the pipeline
    # raises before returning its messages; these are what get stored instead (D30).
    transcripts: dict[str, list[ChatMessage]] = field(default_factory=dict)
    events: list[TraceEvent] = field(default_factory=list)
    # The agent whose transcript AgentDojo scores and `runs.messages` stores.
    scored_agent: str = SINGLE_AGENT
    # (agent, tool call ID) -> who wrote that tool result, when it wasn't the tools: a gate
    # that blocked the call (lab/defenses/gates.py). LabLLM traces the result with that sender.
    tool_result_senders: dict[tuple[str, str], str] = field(default_factory=dict)
    # Request key (first occurrence) -> how many times the run has made that request (D68).
    request_counts: dict[str, int] = field(default_factory=dict)

    # The injection task's check on the final state, set by run_case on attack runs (D49). It
    # lives here, not only in CaseResult, so it survives a ModelError, which has no CaseResult.
    attack_succeeded: bool | None = None
    # The final environment as JSON, and the lab's own attacker_effect on it (D86). Set by
    # run_case for every run that got that far, so they survive a ModelError too.
    final_environment: str | None = None
    attacker_effect: bool | None = None
    # The strings to look for in the trace, set by whoever builds the run. None: not searched,
    # and the run's marker columns stay NULL.
    markers: list[Marker] | None = None

    @property
    def messages(self) -> list[ChatMessage]:
        return self.transcripts.get(self.scored_agent, [])

    @property
    def malformed_tool_calls(self) -> int:
        return sum(call.malformed_tool_calls for call in self.calls)

    @property
    def out_of_scope_tool_calls(self) -> int:
        return sum(call.out_of_scope_tool_calls for call in self.calls)

    @property
    def invalid_argument_tool_calls(self) -> int:
        return sum(call.invalid_argument_tool_calls for call in self.calls)

    @property
    def unparsed_tool_calls(self) -> int:
        return sum(call.unparsed_tool_calls for call in self.calls)

    def rejected_by(self, agent_id: str) -> int:
        """Calls Groq rejected as `tool_use_failed` made by one agent so far (D76, D82: the cap
        is shared by all kinds, per agent per run)."""
        return sum(c.rejected_tool_calls for c in self.calls if c.agent_id == agent_id)
