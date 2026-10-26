"""FakeModel: a ChatClient that replays scripted responses. Used by tests and the CI smoke run.

It never touches the network. Each `complete()` pops the next scripted step; a step can be a
response or an exception (to script a 429, for example). Running out of script is an error,
so a test can't pass by accident after the model "stops talking".

`ScriptedModel` plays the same steps keyed by the request instead, for runs that go through the
response cache (the runner's scripted models).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from lab.models.providers import Limits, ModelInfo, Price, Sampling
from lab.models.types import ModelRequest, ModelResponse, RawToolCall

Step = ModelResponse | Exception

# Experiment configs name the FakeModel as provider "fake". It is not in providers.yaml: it has
# no URL, no key, no limits and no price.
FAKE_PROVIDER = "fake"


def fake_model_info() -> ModelInfo:
    """The FakeModel's entry in place of a providers.yaml one: free, unlimited, temperature 0."""
    checked = date(2026, 10, 25)
    return ModelInfo(
        tool_calling=True,
        price=Price(input_per_mtok=0.0, output_per_mtok=0.0, source="fake", checked=checked),
        limits=Limits(
            rpm=None, rpd=None, tpm=None, tpd=None, source="fake", checked=checked,
            verified_for_free_plan=True,
        ),
        sampling=Sampling(temperature=0.0, reasoning_effort=None, source="fake", checked=checked),
    )


class FakeModel:
    def __init__(self, script: Iterable[Step]) -> None:
        self._script = list(script)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError(f"FakeModel script exhausted after {len(self.requests) - 1} calls")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    @property
    def remaining(self) -> int:
        return len(self._script)


class ScriptedModel:
    """A ChatClient whose response depends only on the request, so a cache hit can't put it out
    of step (D54).

    `scripts` maps each agent's first user message (the user's request, or a worker's
    instruction) to that agent's steps. A request gets step i of its agent's script, where i is
    the number of assistant messages it already holds. FakeModel, by contrast, pops steps in
    call order: when the response cache answers a request instead, the step it would have used
    is left for the next request, and the script falls one step behind.

    `by_system` maps a system prompt to the steps of every agent sent it, for agents whose
    request isn't known in advance (a T3 reviewer is asked about a report or a call). It is
    looked at first.

    `side` answers one-off calls made on an agent's behalf (T5-summary's description of a
    pending call), which share the agent's history and so can't be told apart by it. It gets
    every request first and returns None for those it doesn't answer.
    """

    def __init__(
        self,
        scripts: Mapping[str, Sequence[Step]],
        by_system: Mapping[str, Sequence[Step]] | None = None,
        side: Callable[[ModelRequest], Step | None] | None = None,
    ) -> None:
        self.scripts = {key: list(steps) for key, steps in scripts.items()}
        self.by_system = {key: list(steps) for key, steps in (by_system or {}).items()}
        self.side = side
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        special = self.side(request) if self.side is not None else None
        if special is not None:
            if isinstance(special, Exception):
                raise special
            return special
        system: str | None = next(
            (m["content"] for m in request.messages if m["role"] == "system"), None
        )
        first_user: str | None = next(
            (m["content"] for m in request.messages if m["role"] == "user"), None
        )
        steps = self.by_system.get(system) if system is not None else None
        if steps is None and first_user is not None:
            steps = self.scripts.get(first_user)
        if steps is None:
            raise AssertionError(f"No script for an agent asked {first_user!r}")
        i = sum(1 for m in request.messages if m["role"] == "assistant")
        if i >= len(steps):
            raise AssertionError(f"Script exhausted after {len(steps)} steps for {first_user!r}")
        step = steps[i]
        if isinstance(step, Exception):
            raise step
        return step


def answer(text: str | None) -> ModelResponse:
    """A final answer. `None` scripts an assistant turn with no content."""
    return _response(content=text, tool_calls=[], finish_reason="stop")


def call(name: str, args: dict[str, Any], *, call_id: str | None = None) -> ModelResponse:
    """One well-formed tool call."""
    return calls((name, json.dumps(args), call_id))


def malformed_call(name: str, raw_arguments: str, *, call_id: str | None = None) -> ModelResponse:
    """One tool call whose arguments are given as raw (possibly invalid) JSON text."""
    return calls((name, raw_arguments, call_id))


def calls(*specs: tuple[str, str, str | None]) -> ModelResponse:
    """Several tool calls in one turn, each as (name, raw_arguments, call_id or None)."""
    tool_calls = [
        RawToolCall(id=call_id or f"call_{name}_{i}", name=name, arguments=raw)
        for i, (name, raw, call_id) in enumerate(specs)
    ]
    return _response(content=None, tool_calls=tool_calls, finish_reason="tool_calls")


def _response(
    *, content: str | None, tool_calls: list[RawToolCall], finish_reason: str
) -> ModelResponse:
    # Fixed small numbers so token and cost accounting can be tested end to end.
    return ModelResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        input_tokens=10,
        output_tokens=5,
        latency_s=0.0,
        cost_usd=0.0,
    )
