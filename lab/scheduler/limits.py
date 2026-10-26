"""Per-model rate limiter: requests and tokens, per minute and per day (DECISIONS.md D22).

The limiter keeps a log of every request it let through, as (time, tokens), and checks four
sliding windows before letting the next one go:

    requests in the last 60 s  <= rpm      tokens in the last 60 s  <= tpm
    requests in the last 24 h  <= rpd      tokens in the last 24 h  <= tpd

A request's tokens are first counted at an estimate made before sending (`estimate_tokens`),
then corrected to the provider's reported usage when the response arrives (`settle`).

Groq also sends headers for two of the four limits (remaining requests per day, remaining
tokens per minute; D20). When the last response carried them, the limiter obeys whichever is
stricter: its own count or the provider's.

If the next request would have to wait longer than `max_wait_s` (in practice, only when a daily
limit is used up), `acquire` raises DailyCapReached instead of sleeping, so the scheduler can
stop cleanly and a later invocation can resume.
"""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from lab.errors import DailyCapReached
from lab.models.providers import Limits
from lab.models.types import ModelRequest, RateLimitHeaders
from lab.scheduler.clock import Clock

MINUTE_S = 60.0
DAY_S = 86_400.0
DEFAULT_MAX_WAIT_S = 120.0

# Calibrated on the 111 Groq calls of the milestone-1 baseline (D42): 7.4 characters of request
# JSON per prompt token overall, 5.5 for the densest call. 5.0 sits below every call measured,
# so the estimate errs high (D22). It is only the pre-send guess: after the call it is replaced
# by the reported usage, and Groq's remaining-tokens header still overrides it when stricter.
# The one-tool probe gave 2.6 (D20), which made earlier estimates about 3x too high (D41).
CHARS_PER_TOKEN = 5.0
# How Groq charges tokens/minute, from the 2026-10-25 max_tokens probe (D28): at admission it
# takes prompt + min(max_tokens, its own output estimate). With max_tokens=256 the header drop
# was exactly prompt + 256; with 2048 it was prompt + 324 and prompt + 409, nowhere near 2048.
# So we reserve min(max_tokens, OUTPUT_RESERVE), with OUTPUT_RESERVE above the largest
# estimate seen.
OUTPUT_RESERVE = 512


@dataclass(frozen=True)
class TokenEstimate:
    input_tokens: int
    total_tokens: int  # input plus the output reserve


def estimate_tokens(request: ModelRequest) -> TokenEstimate:
    chars = len(json.dumps(request.messages)) + len(json.dumps(request.tools))
    if request.response_format is not None:
        chars += len(json.dumps(request.response_format))  # a schema is sent as input too
    input_tokens = math.ceil(chars / CHARS_PER_TOKEN)
    reserve = (
        OUTPUT_RESERVE if request.max_tokens is None else min(request.max_tokens, OUTPUT_RESERVE)
    )
    return TokenEstimate(input_tokens=input_tokens, total_tokens=input_tokens + reserve)


@dataclass
class SentRequest:
    """One request in the limiter's log. `tokens` starts as the estimate and is corrected."""

    ts: float
    tokens: int


class RateLimiter:
    def __init__(
        self,
        provider: str,
        model_id: str,
        limits: Limits,
        clock: Clock,
        *,
        max_wait_s: float = DEFAULT_MAX_WAIT_S,
        history: Iterable[tuple[float, int]] = (),
    ) -> None:
        """`history` is (time, tokens) for requests already sent, e.g. by an earlier
        invocation, read back from the run store. Without it a restart would forget the day.
        """
        self.provider = provider
        self.model_id = model_id
        self.limits = limits
        self.clock = clock
        self.max_wait_s = max_wait_s
        self._log: deque[SentRequest] = deque(
            SentRequest(ts, tokens) for ts, tokens in sorted(history)
        )
        self._headers: RateLimitHeaders | None = None
        self._headers_at = 0.0

    def acquire(self, estimated_tokens: int) -> SentRequest:
        """Block until a request of `estimated_tokens` fits every limit, then log it."""
        while True:
            wait_s, limit = self._wait_needed(estimated_tokens)
            if wait_s <= 0:
                break
            if wait_s > self.max_wait_s:
                raise DailyCapReached(
                    f"{self.provider}/{self.model_id}: {limit} limit reached; "
                    f"next request possible in {wait_s:.0f} s",
                    provider=self.provider,
                    model_id=self.model_id,
                    wait_s=wait_s,
                )
            self.clock.sleep(wait_s)
        sent = SentRequest(self.clock.now(), estimated_tokens)
        self._log.append(sent)
        return sent

    def settle(
        self, sent: SentRequest, tokens: int, headers: RateLimitHeaders | None = None
    ) -> None:
        """Replace the estimate with what the request really used; remember fresh headers."""
        sent.tokens = tokens
        if headers is not None:
            self._headers = headers
            self._headers_at = self.clock.now()

    def _wait_needed(self, estimated_tokens: int) -> tuple[float, str]:
        """Seconds until the request fits, and the name of the limit that is binding."""
        now = self.clock.now()
        while self._log and self._log[0].ts <= now - DAY_S:
            self._log.popleft()
        limits = self.limits
        waits = [(0.0, "")]
        for window_s, max_requests, max_tokens, name in (
            (MINUTE_S, limits.rpm, limits.tpm, "minute"),
            (DAY_S, limits.rpd, limits.tpd, "day"),
        ):
            in_window = [r for r in self._log if r.ts > now - window_s]
            if max_requests is not None:
                wait = _wait_until_count_fits(in_window, max_requests - 1, window_s, now)
                waits.append((wait, f"requests/{name}"))
            if max_tokens is not None:
                # A request bigger than the whole budget waits for an empty window and is then
                # sent; the provider decides. Otherwise it could never run.
                allowed = max_tokens - min(estimated_tokens, max_tokens)
                wait = _wait_until_tokens_fit(in_window, allowed, window_s, now)
                waits.append((wait, f"tokens/{name}"))
        waits.extend(self._header_waits(estimated_tokens, now))
        return max(waits)

    def _header_waits(self, estimated_tokens: int, now: float) -> list[tuple[float, str]]:
        h = self._headers
        if h is None:
            return []
        elapsed = now - self._headers_at
        waits = []
        if (
            h.remaining_tokens_per_minute is not None
            and h.tokens_reset_s is not None
            and h.remaining_tokens_per_minute < estimated_tokens
            and elapsed < h.tokens_reset_s
        ):
            waits.append((h.tokens_reset_s - elapsed, "tokens/minute (provider header)"))
        if (
            h.remaining_requests_per_day is not None
            and h.requests_reset_s is not None
            and h.remaining_requests_per_day < 1
            and elapsed < h.requests_reset_s
        ):
            waits.append((h.requests_reset_s - elapsed, "requests/day (provider header)"))
        return waits


def _wait_until_count_fits(
    in_window: list[SentRequest], allowed: int, window_s: float, now: float
) -> float:
    """Wait until at most `allowed` logged requests remain inside the window."""
    excess = len(in_window) - max(allowed, 0)
    if excess <= 0:
        return 0.0
    # The oldest `excess` requests must leave the window; the last of them leaves last.
    return in_window[excess - 1].ts + window_s - now


def _wait_until_tokens_fit(
    in_window: list[SentRequest], allowed: int, window_s: float, now: float
) -> float:
    """Wait until the logged tokens inside the window are at most `allowed`."""
    total = sum(r.tokens for r in in_window)
    if total <= allowed:
        return 0.0
    for request in in_window:  # oldest first: drop requests until the rest fit
        total -= request.tokens
        if total <= allowed:
            return request.ts + window_s - now
    return math.inf  # unreachable while allowed >= 0: an empty window always fits
