"""The one retry policy, and the client wrapper that applies it (DECISIONS.md D23).

`ScheduledClient` sits between the cache and the provider client:

    LabLLM -> CachingClient -> ScheduledClient -> OpenAICompatClient

so a cache hit never waits for, or uses up, a rate limit. Each attempt goes through the
model's RateLimiter and is logged. Only InfraError is retried; ModelError and ConfigError
would fail the same way again, so they go straight up.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from lab.errors import ConfigError, DailyCapReached, InfraError
from lab.models.client import ChatClient
from lab.models.types import ModelRequest, ModelResponse
from lab.redact import redact
from lab.scheduler.clock import Clock
from lab.scheduler.limits import RateLimiter, SentRequest, estimate_tokens


class RetryPolicy:
    """Exponential backoff with jitter, for InfraError only.

    Attempt n (1-based) failed: wait a random time in [d/2, d], where d = min(cap, base * 2^(n-1)).
    Random jitter stops retries from lining up. If the provider sent Retry-After, wait at
    least that long.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        base_s: float = 2.0,
        cap_s: float = 60.0,
        rng: random.Random | None = None,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_s = base_s
        self.cap_s = cap_s
        self.rng = rng if rng is not None else random.Random()

    def delay_s(self, attempt: int, retry_after_s: float | None) -> float:
        ceiling = min(self.cap_s, self.base_s * 2.0 ** (attempt - 1))
        backoff = ceiling / 2 + self.rng.uniform(0, ceiling / 2)
        return max(backoff, retry_after_s or 0.0)


@dataclass(frozen=True)
class RequestLogEntry:
    """One real request to a provider, successful or not. Written to the run store."""

    ts: float
    provider: str
    model_id: str
    status: str  # "ok", "http_429", "http_503", "network", "model_error", "config_error"
    tokens: int  # what it counts against the limits
    # For a failed request (D42): our error class, the HTTP status if there was a response, and
    # the provider's message with anything key-like redacted. All None for "ok".
    error_type: str | None = None
    http_status: int | None = None
    error_message: str | None = None

    @classmethod
    def failure(
        cls, ts: float, provider: str, model_id: str, status: str, tokens: int, error: Exception
    ) -> RequestLogEntry:
        return cls(
            ts=ts,
            provider=provider,
            model_id=model_id,
            status=status,
            tokens=tokens,
            error_type=type(error).__name__,
            http_status=getattr(error, "status_code", None),
            # The client already redacts with the real key; this second pass covers any other
            # client and keeps the rule in one place for whatever reaches the store.
            error_message=redact(str(error)),
        )


RequestLog = Callable[[RequestLogEntry], None]


class ScheduledClient:
    def __init__(
        self,
        inner: ChatClient,
        limiter: RateLimiter,
        clock: Clock,
        *,
        retry: RetryPolicy | None = None,
        log: RequestLog | None = None,
    ) -> None:
        self.inner = inner
        self.limiter = limiter
        self.clock = clock
        self.retry = retry if retry is not None else RetryPolicy()
        self.log = log

    def complete(self, request: ModelRequest) -> ModelResponse:
        estimate = estimate_tokens(request)
        for attempt in range(1, self.retry.max_attempts + 1):
            sent = self.limiter.acquire(estimate.total_tokens)
            try:
                response = self.inner.complete(request)
            except InfraError as e:
                # A 429 was refused before any work; other failures (5xx, timeouts) may have
                # processed the prompt, so count it to stay on the safe side.
                used = 0 if e.status_code == 429 else estimate.input_tokens
                self._settle(sent, used, _infra_status(e), error=e)
                if attempt == self.retry.max_attempts:
                    raise
                delay = self.retry.delay_s(attempt, e.retry_after_s)
                if delay > self.limiter.max_wait_s:
                    # e.g. a 429 for tokens/day says "try again in 20 minutes": stop, resume later.
                    raise DailyCapReached(
                        f"{request.provider}/{request.model_id}: provider asked to wait "
                        f"{delay:.0f} s ({e})",
                        provider=request.provider,
                        model_id=request.model_id,
                        wait_s=delay,
                    ) from e
                self.clock.sleep(delay)
                continue
            except Exception as e:
                status = "config_error" if isinstance(e, ConfigError) else "model_error"
                self._settle(sent, estimate.input_tokens, status, error=e)
                raise
            reported = _reported_tokens(response)
            tokens = reported if reported is not None else estimate.total_tokens
            self._settle(sent, tokens, "ok", response)
            return response.model_copy(update={"attempts": attempt})
        raise AssertionError("unreachable: the last attempt either returns or raises")

    def _settle(
        self,
        sent: SentRequest,
        tokens: int,
        status: str,
        response: ModelResponse | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.limiter.settle(sent, tokens, response.rate_limits if response else None)
        if self.log is None:
            return
        fields = (self.clock.now(), self.limiter.provider, self.limiter.model_id, status, tokens)
        if error is None:
            self.log(RequestLogEntry(*fields))
        else:
            self.log(RequestLogEntry.failure(*fields, error=error))


def _infra_status(e: InfraError) -> str:
    return f"http_{e.status_code}" if e.status_code is not None else "network"


def _reported_tokens(response: ModelResponse) -> int | None:
    if response.input_tokens is None or response.output_tokens is None:
        return None
    return response.input_tokens + response.output_tokens
