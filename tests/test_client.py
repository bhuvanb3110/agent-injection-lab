"""OpenAICompatClient against a mock HTTP transport: no sockets are opened."""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from lab.errors import ConfigError, InfraError, ModelError
from lab.models.client import OpenAICompatClient
from lab.models.providers import load_providers
from lab.models.types import ModelRequest

SECRET = "gsk_test_secret_value_do_not_leak_123"
MODEL = "openai/gpt-oss-20b"

MILLION_IN_TWO_MILLION_OUT = {
    "prompt_tokens": 1_000_000,
    "completion_tokens": 2_000_000,
    "total_tokens": 3_000_000,
}

Handler = Callable[[httpx2.Request], httpx2.Response]


def completion(message: dict[str, Any], usage: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": usage if usage is not None else MILLION_IN_TWO_MILLION_OUT,
    }


class Recorder:
    """Mock transport handler that records every HTTP request it sees."""

    def __init__(self, respond: Handler) -> None:
        self.respond = respond
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.respond(request)

    def body(self, i: int = 0) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.requests[i].content)
        return result


def make_client(respond: Handler) -> tuple[OpenAICompatClient, Recorder]:
    recorder = Recorder(respond)
    client = OpenAICompatClient(
        load_providers()["groq"],
        http_client=httpx2.Client(transport=httpx2.MockTransport(recorder)),
        environ={"GROQ_API_KEY": SECRET},
    )
    return client, recorder


def request(**overrides: Any) -> ModelRequest:
    fields: dict[str, Any] = {
        "provider": "groq",
        "model_id": MODEL,
        "messages": [
            {"role": "system", "content": "You are a bank assistant."},
            {"role": "user", "content": "What is my balance?"},
        ],
        "tools": [],
        "temperature": 0.0,
    }
    return ModelRequest(**(fields | overrides))


def status(code: int, headers: dict[str, str] | None = None) -> Handler:
    return lambda _: httpx2.Response(code, json={"error": {"message": "nope"}}, headers=headers)


def test_sdk_retries_are_off() -> None:
    client, recorder = make_client(status(500))
    assert client.max_retries == 0
    with pytest.raises(InfraError):
        client.complete(request())
    assert len(recorder.requests) == 1  # the SDK default would have made 3


def test_429_raises_infra_error_with_retry_after() -> None:
    client, _ = make_client(status(429, headers={"retry-after": "7"}))
    with pytest.raises(InfraError) as info:
        client.complete(request())
    assert info.value.status_code == 429
    assert info.value.retry_after_s == 7.0


@pytest.mark.parametrize("code", [500, 502, 503])
def test_5xx_raises_infra_error(code: int) -> None:
    client, _ = make_client(status(code))
    with pytest.raises(InfraError) as info:
        client.complete(request())
    assert info.value.status_code == code


@pytest.mark.parametrize(
    "exc", [httpx2.ReadTimeout("slow"), httpx2.ConnectError("refused")], ids=["timeout", "connect"]
)
def test_timeout_and_connection_errors_raise_infra_error(exc: Exception) -> None:
    def fail(_: httpx2.Request) -> httpx2.Response:
        raise exc

    client, _ = make_client(fail)
    with pytest.raises(InfraError) as info:
        client.complete(request())
    assert info.value.status_code is None


def test_400_raises_model_error() -> None:
    client, _ = make_client(status(400))
    with pytest.raises(ModelError) as info:
        client.complete(request())
    assert info.value.status_code == 400


@pytest.mark.parametrize("code", [401, 403, 404])
def test_auth_and_not_found_raise_config_error(code: int) -> None:
    client, _ = make_client(status(code))
    with pytest.raises(ConfigError):
        client.complete(request())


def test_missing_key_names_the_variable() -> None:
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        OpenAICompatClient(load_providers()["groq"], environ={})


def test_unknown_model_is_a_config_error() -> None:
    client, recorder = make_client(status(200))
    with pytest.raises(ConfigError, match="not in configs/providers.yaml"):
        client.complete(request(model_id="llama-3.3-70b-versatile"))
    assert recorder.requests == []


def test_sends_temperature_zero_and_system_role_and_no_tool_choice() -> None:
    client, recorder = make_client(
        lambda _: httpx2.Response(200, json=completion({"role": "assistant", "content": "hi"}))
    )
    tool = {"type": "function", "function": {"name": "get_balance", "parameters": {}}}
    client.complete(request(tools=[tool]))

    body = recorder.body()
    assert body["temperature"] == 0.0  # AgentDojo's OpenAILLM drops 0
    assert body["messages"][0]["role"] == "system"
    assert body["tools"] == [tool]
    assert "tool_choice" not in body
    assert "max_tokens" not in body
    assert "repeat_idx" not in body  # cache-only field, never sent
    assert SECRET not in recorder.requests[0].content.decode()


def test_parses_content_tool_calls_tokens_and_cost() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "reasoning": "I should check the balance.",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_balance", "arguments": "{}"},
            }
        ],
    }
    client, _ = make_client(lambda _: httpx2.Response(200, json=completion(message)))
    response = client.complete(request())

    assert response.content is None
    assert [(c.id, c.name, c.arguments) for c in response.tool_calls] == [
        ("call_1", "get_balance", "{}")
    ]
    assert response.reasoning == "I should check the balance."
    assert (response.input_tokens, response.output_tokens) == (1_000_000, 2_000_000)
    # gpt-oss-20b on Groq: $0.075/Mtok in, $0.30/Mtok out (configs/providers.yaml)
    assert response.cost_usd == pytest.approx(0.075 + 2 * 0.30)
    assert response.latency_s >= 0
    assert not response.cached
