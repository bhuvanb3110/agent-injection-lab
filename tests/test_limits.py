"""RateLimiter on a fake clock: four sliding windows, provider headers, daily caps."""

from datetime import date

import pytest

from lab.errors import DailyCapReached
from lab.models.providers import Limits
from lab.models.types import ModelRequest, RateLimitHeaders
from lab.scheduler.clock import FakeClock
from lab.scheduler.limits import (
    DAY_S,
    OUTPUT_RESERVE,
    RateLimiter,
    estimate_tokens,
)


def limits(
    rpm: int | None = None, rpd: int | None = None, tpm: int | None = None, tpd: int | None = None
) -> Limits:
    return Limits(
        rpm=rpm,
        rpd=rpd,
        tpm=tpm,
        tpd=tpd,
        source="test",
        checked=date(2026, 10, 25),
        verified_for_free_plan=True,
    )


def limiter(lim: Limits, clock: FakeClock, **kwargs: object) -> RateLimiter:
    return RateLimiter("groq", "openai/gpt-oss-20b", lim, clock, **kwargs)  # type: ignore[arg-type]


def test_requests_per_minute_waits_for_the_oldest_to_leave_the_window() -> None:
    clock = FakeClock()
    rl = limiter(limits(rpm=2), clock)
    rl.acquire(1)
    clock.advance(10)
    rl.acquire(1)
    rl.acquire(1)  # the first request is 10 s old: 50 s until it leaves the window
    assert clock.sleeps == [pytest.approx(50.0)]


def test_tokens_per_minute_counts_the_settled_value_not_the_estimate() -> None:
    clock = FakeClock()
    rl = limiter(limits(tpm=1000), clock)
    first = rl.acquire(900)  # estimate
    rl.settle(first, 100)  # the call really used 100
    rl.acquire(800)  # 100 + 800 fits
    assert clock.sleeps == []
    rl.acquire(600)  # 900 + 600 does not: wait until both leave
    assert clock.sleeps == [pytest.approx(60.0)]


def test_requests_per_day_cap_raises_instead_of_sleeping() -> None:
    clock = FakeClock()
    rl = limiter(limits(rpd=2), clock)
    rl.acquire(1)
    rl.acquire(1)
    with pytest.raises(DailyCapReached) as info:
        rl.acquire(1)
    assert "requests/day" in str(info.value)
    assert info.value.wait_s == pytest.approx(DAY_S)
    assert clock.sleeps == []


def test_tokens_per_day_cap_survives_a_restart_through_history() -> None:
    clock = FakeClock()
    # An earlier invocation used 900 of 1000 tokens/day, 100 s ago.
    history = [(clock.now() - 100, 900)]
    rl = limiter(limits(tpd=1000), clock, history=history)
    with pytest.raises(DailyCapReached, match="tokens/day"):
        rl.acquire(200)
    clock.advance(DAY_S)
    rl.acquire(200)  # the old usage has left the 24 h window


def test_request_larger_than_tpm_waits_for_an_empty_minute_then_goes() -> None:
    clock = FakeClock()
    rl = limiter(limits(tpm=1000), clock)
    rl.settle(rl.acquire(10), 10)
    rl.acquire(5000)  # can never "fit"; sent once the window is empty
    assert clock.sleeps == [pytest.approx(60.0)]


def test_provider_header_for_tokens_per_minute_is_obeyed() -> None:
    clock = FakeClock()
    rl = limiter(limits(tpm=8000), clock)  # our own count says there is plenty of room
    first = rl.acquire(100)
    rl.settle(
        first, 100, RateLimitHeaders(remaining_tokens_per_minute=300, tokens_reset_s=5.0)
    )
    rl.acquire(500)  # the provider says only 300 are left until its window resets
    assert clock.sleeps == [pytest.approx(5.0)]


def test_provider_header_for_requests_per_day_is_obeyed() -> None:
    clock = FakeClock()
    rl = limiter(limits(rpd=1000), clock)
    first = rl.acquire(1)
    rl.settle(first, 1, RateLimitHeaders(remaining_requests_per_day=0, requests_reset_s=86.4))
    rl.acquire(1)  # Groq refills one request every 86.4 s: short enough to wait for
    assert clock.sleeps == [pytest.approx(86.4)]


def test_no_limits_never_waits() -> None:
    clock = FakeClock()
    rl = limiter(limits(), clock)  # local Ollama
    for _ in range(100):
        rl.settle(rl.acquire(10_000), 10_000)
    assert clock.sleeps == []


def test_estimate_reserves_output_like_groq_does() -> None:
    # Groq charges prompt + min(max_tokens, its output estimate), not prompt + max_tokens (D28).
    base = {
        "provider": "groq",
        "model_id": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "x" * 1000}],
        "tools": [],
        "temperature": 0.6,
    }
    unset = estimate_tokens(ModelRequest(**base))  # type: ignore[arg-type]
    small = estimate_tokens(ModelRequest(**base, max_tokens=50))  # type: ignore[arg-type]
    large = estimate_tokens(ModelRequest(**base, max_tokens=2048))  # type: ignore[arg-type]
    # Errs high: the densest baseline call was 5.5 characters per token (D42).
    assert unset.input_tokens == small.input_tokens > 1000 / 5.5
    assert unset.total_tokens == unset.input_tokens + OUTPUT_RESERVE
    assert small.total_tokens == small.input_tokens + 50
    assert large.total_tokens == large.input_tokens + OUTPUT_RESERVE
