"""Response cache: stable keys, no secrets, hit and miss."""

import contextlib
import json
from pathlib import Path
from typing import Any

import httpx2

from lab.models.cache import CachingClient, ResponseCache, request_key
from lab.models.client import OpenAICompatClient
from lab.models.fake import FakeModel, answer
from lab.models.providers import load_providers
from lab.models.types import ModelRequest, ModelResponse, RateLimitHeaders

SECRET = "gsk_test_secret_value_do_not_leak_123"

TOOL = {
    "type": "function",
    "function": {
        "name": "send_money",
        "description": "Send money.",
        "parameters": {"type": "object", "properties": {"amount": {"type": "number"}}},
    },
}


def request(**overrides: Any) -> ModelRequest:
    fields: dict[str, Any] = {
        "provider": "groq",
        "model_id": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "Pay my bill."}],
        "tools": [TOOL],
        "temperature": 0.0,
    }
    return ModelRequest(**(fields | overrides))


def reversed_dict(value: Any) -> Any:
    """The same data with every dict's keys in reverse insertion order."""
    if isinstance(value, dict):
        return {k: reversed_dict(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_dict(v) for v in value]
    return value


def test_key_is_stable_when_dict_key_order_changes() -> None:
    original = request()
    shuffled = request(
        messages=reversed_dict(original.messages), tools=reversed_dict(original.tools)
    )
    assert json.dumps(shuffled.tools) != json.dumps(original.tools)  # really reordered
    assert request_key(shuffled) == request_key(original)


def test_key_changes_with_every_field_that_matters() -> None:
    base = request_key(request())
    variants = [
        request(provider="ollama"),
        request(model_id="openai/gpt-oss-120b"),
        request(messages=[{"role": "user", "content": "Pay my rent."}]),
        request(tools=[]),
        request(temperature=0.7),
        request(max_tokens=256),
        request(repeat_idx=1),  # repeats are independent samples, not replays
        request(reasoning_effort="medium"),
    ]
    keys = {request_key(v) for v in variants}
    assert base not in keys
    assert len(keys) == len(variants)


def test_miss_then_hit_keeps_original_latency_and_tokens(tmp_path: Path) -> None:
    original = answer("Your balance is 1810.").model_copy(
        update={"latency_s": 1.25, "input_tokens": 321, "output_tokens": 12, "cost_usd": 0.001}
    )
    fake = FakeModel([original])
    client = CachingClient(fake, ResponseCache(tmp_path / "cache.sqlite"))

    first = client.complete(request())
    second = client.complete(request())

    assert len(fake.requests) == 1  # the second call never reached the model
    assert not first.cached
    assert second.cached
    assert second.model_dump(exclude={"cached"}) == first.model_dump(exclude={"cached"})
    assert (second.latency_s, second.input_tokens, second.output_tokens) == (1.25, 321, 12)


def test_hit_survives_reopening_the_file(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    first = ResponseCache(path)
    first.put(request(), answer("ok"))
    first.close()

    hit = ResponseCache(path).get(request())
    assert hit is not None and hit.cached and hit.content == "ok"


def test_errors_are_not_cached(tmp_path: Path) -> None:
    fake = FakeModel([RuntimeError("boom"), answer("ok")])
    client = CachingClient(fake, ResponseCache(tmp_path / "cache.sqlite"))
    with contextlib.suppress(RuntimeError):
        client.complete(request())
    assert not client.complete(request()).cached
    assert len(fake.requests) == 2


def test_secret_never_reaches_key_or_cache_file(tmp_path: Path) -> None:
    def respond(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "openai/gpt-oss-20b",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )

    real = OpenAICompatClient(
        load_providers()["groq"],
        http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
        environ={"GROQ_API_KEY": SECRET},
    )
    path = tmp_path / "cache.sqlite"
    cache = ResponseCache(path)
    req = request()
    response: ModelResponse = CachingClient(real, cache).complete(req)
    cache.close()

    assert response.content == "hi"
    assert SECRET not in request_key(req)
    on_disk = b"".join(p.read_bytes() for p in tmp_path.iterdir())  # db plus any WAL files
    assert b"Pay my bill." in on_disk  # sanity: the request really was written
    assert SECRET.encode() not in on_disk


def test_rate_limit_headers_are_not_cached(tmp_path: Path) -> None:
    # Headers describe the provider at call time, not the response; a hit must not replay them.
    cache = ResponseCache(tmp_path / "cache.sqlite")
    with_headers = answer("hi").model_copy(
        update={"rate_limits": RateLimitHeaders(remaining_tokens_per_minute=5)}
    )
    cache.put(request(), with_headers)
    hit = cache.get(request())
    assert hit is not None and hit.rate_limits is None
    cache.close()
