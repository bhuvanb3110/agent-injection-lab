"""ScheduledClient: limiter + one retry policy (InfraError only) + request log."""

import random
from datetime import date

import pytest

from lab.errors import ConfigError, DailyCapReached, InfraError, ModelError
from lab.models.fake import FakeModel, answer
from lab.models.providers import Limits
from lab.models.types import ModelRequest
from lab.scheduler.clock import FakeClock
from lab.scheduler.limits import RateLimiter
from lab.scheduler.retry import RequestLogEntry, RetryPolicy, ScheduledClient

NO_LIMITS = Limits(
    rpm=None,
    rpd=None,
    tpm=None,
    tpd=None,
    source="test",
    checked=date(2026, 10, 25),
    verified_for_free_plan=True,
)
REQUEST = ModelRequest(
    provider="groq",
    model_id="openai/gpt-oss-20b",
    messages=[{"role": "user", "content": "hi"}],
    tools=[],
    temperature=0.6,
)


def http(code: int, retry_after_s: float | None = None) -> InfraError:
    return InfraError(
        f"HTTP {code}", provider="groq", status_code=code, retry_after_s=retry_after_s
    )


def make(
    script: list[object], limits: Limits = NO_LIMITS
) -> tuple[ScheduledClient, FakeClock, list[RequestLogEntry]]:
    clock = FakeClock()
    log: list[RequestLogEntry] = []
    client = ScheduledClient(
        FakeModel(script),  # type: ignore[arg-type]
        RateLimiter("groq", "openai/gpt-oss-20b", limits, clock),
        clock,
        retry=RetryPolicy(rng=random.Random(0)),
        log=log.append,
    )
    return client, clock, log


def test_retries_429_then_succeeds() -> None:
    client, clock, log = make([http(429, retry_after_s=3.0), answer("hello")])
    response = client.complete(REQUEST)

    assert response.content == "hello"
    assert response.attempts == 2
    assert len(clock.sleeps) == 1 and clock.sleeps[0] >= 3.0  # Retry-After is a floor
    assert [e.status for e in log] == ["http_429", "ok"]
    assert log[0].tokens == 0  # a 429 used nothing
    assert log[1].tokens == 15  # FakeModel reports 10 in + 5 out


def test_gives_up_after_max_attempts_with_exponential_jittered_backoff() -> None:
    client, clock, log = make([http(503)] * 5)
    with pytest.raises(InfraError):
        client.complete(REQUEST)

    assert [e.status for e in log] == ["http_503"] * 5
    assert len(clock.sleeps) == 4  # between attempts, not after the last
    for attempt, delay in enumerate(clock.sleeps, start=1):
        ceiling = min(60.0, 2.0 * 2 ** (attempt - 1))
        assert ceiling / 2 <= delay <= ceiling


@pytest.mark.parametrize(
    "error",
    [
        ModelError("HTTP 400", provider="groq", subtype="http_400", status_code=400),
        ConfigError("HTTP 401"),
    ],
)
def test_model_and_config_errors_are_not_retried(error: Exception) -> None:
    client, clock, log = make([error])
    with pytest.raises(type(error)):
        client.complete(REQUEST)
    assert len(log) == 1
    assert clock.sleeps == []


def test_long_retry_after_stops_the_model_instead_of_sleeping() -> None:
    # e.g. a tokens/day 429 that says "try again in 1 hour"
    client, clock, log = make([http(429, retry_after_s=3600.0)])
    with pytest.raises(DailyCapReached) as info:
        client.complete(REQUEST)
    assert info.value.model_id == "openai/gpt-oss-20b"
    assert clock.sleeps == []
    assert len(log) == 1


def test_failed_requests_log_error_type_status_and_redacted_message() -> None:
    key = "gsk_AbCdEf0123456789XyZaBcDeF0123456789"
    client, _, log = make([
        http(503),
        ModelError(f"HTTP 400 from groq: bad request for {key}", provider="groq",
                   subtype="http_400", status_code=400),
    ])
    with pytest.raises(ModelError):
        client.complete(REQUEST)
    assert [(e.error_type, e.http_status) for e in log] == [
        ("InfraError", 503), ("ModelError", 400)
    ]
    assert log[0].error_message == "HTTP 503"
    assert log[1].error_message is not None
    assert key not in log[1].error_message and "[REDACTED]" in log[1].error_message


def test_config_error_logs_its_status_and_success_logs_no_error() -> None:
    client, _, log = make([answer("ok"), ConfigError("HTTP 401 from groq: no", status_code=401)])
    client.complete(REQUEST)
    with pytest.raises(ConfigError):
        client.complete(REQUEST)
    assert (log[0].error_type, log[0].http_status, log[0].error_message) == (None, None, None)
    assert (log[1].status, log[1].error_type, log[1].http_status) == (
        "config_error", "ConfigError", 401
    )


def test_network_errors_are_logged_as_network() -> None:
    client, _, log = make([InfraError("timeout", provider="groq"), answer("ok")])
    client.complete(REQUEST)
    assert [e.status for e in log] == ["network", "ok"]
