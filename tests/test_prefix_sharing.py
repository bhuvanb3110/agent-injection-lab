"""prefix_divergence and common random numbers between nested topologies (PLAN_M2 §7, D66).

A counter-based model gives a different response on every *real* call (its tool-call IDs and
texts carry the count), so two runs that show the same responses shared them through the
cache. Runs go through `lab run`, so the scheduler records the rows."""

import itertools
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

import lab.runner
from lab.cli import main
from lab.models.fake import ScriptedModel
from lab.models.types import ModelRequest, ModelResponse
from lab.store import RunRecord, RunStore, prefix_divergence
from lab.tracing.markers import summarise

USER_TASK = "user_task_2"
INJECTION_TASK = "injection_task_4"


class Counting:
    """Wraps a ScriptedModel: every real call gets its own number, in its tool-call IDs and in
    any text that isn't a JSON verdict (which must stay parseable)."""

    def __init__(self, inner: ScriptedModel, counter: Iterator[int]) -> None:
        self.inner = inner
        self.counter = counter

    def complete(self, request: ModelRequest) -> ModelResponse:
        response = self.inner.complete(request)
        n = next(self.counter)
        content = response.content
        if content is not None and not content.lstrip().startswith("{"):
            content = f"{content} #{n}"
        calls = [c.model_copy(update={"id": f"{c.id}~{n}"}) for c in response.tool_calls]
        return response.model_copy(update={"content": content, "tool_calls": calls})


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = itertools.count()

    def build(*args: Any, **kwargs: Any) -> Counting:
        return Counting(ScriptedModel(*args, **kwargs), counter)

    monkeypatch.setattr(lab.runner, "ScriptedModel", build)


def run(tmp_path: Path, name: str, topologies: list[str], models: list[str]) -> RunStore:
    """One `lab run` on a fresh store and a fresh cache."""
    folder = tmp_path / name
    folder.mkdir()
    config = folder / "config.yaml"
    config.write_text(yaml.safe_dump({
        "name": "prefix", "suite": "banking", "topologies": topologies,
        "user_tasks": [USER_TASK],
        "models": [{"provider": "fake", "model_id": m} for m in models],
        "repeats": 1, "seed": 5, "max_tokens": 2048,
        "attack": {"injection_tasks": [INJECTION_TASK]},
    }))
    assert main(["run", str(config), "--store", str(folder / "runs.sqlite"), "--cache",
                 str(folder / "cache.sqlite"), "--env-file", str(folder / "no.env")]) == 0
    return RunStore(folder / "runs.sqlite")


def by_case(store: RunStore) -> dict[tuple[str, str | None, str], RunRecord]:
    runs = [store.get_run(r) for r in store.run_ids("prefix")]
    return {(r.spec.model_id, r.spec.injection_task_id, r.spec.topology): r for r in runs}


def unnumbered(value: Any) -> str:
    """The value as JSON, with the counter's numbers taken out."""
    return re.sub(r"(~\d+)|( #\d+)", "", json.dumps(value, sort_keys=True))


def agent_messages(run: RunRecord, n_calls: int) -> list[dict[str, Any]]:
    """The model replies of a run's first `n_calls` calls."""
    return [e.payload for e in run.events
            if e.kind == "message" and e.call_idx is not None and e.call_idx < n_calls]


def test_the_divergence_is_computed_from_request_keys_only() -> None:
    assert prefix_divergence("c", "p", ["a", "b"], ["a", "b"]).divergence_call_idx is None
    row = prefix_divergence("c", "p", ["a", "x", "b"], ["a", "b"])
    assert (row.divergence_call_idx, row.n_shared_calls) == (1, 1)
    row = prefix_divergence("c", "p", ["a", "b"], ["a", "b", "c"])  # the child stopped early
    assert (row.divergence_call_idx, row.n_shared_calls) == (2, 2)


@pytest.mark.usefixtures("counting")
def test_t2_then_t3_and_t3_then_t2_give_the_same_runs_and_divergence(tmp_path: Path) -> None:
    first = run(tmp_path, "t2_first", ["t2", "t3"], ["obedient", "ground-truth"])
    second = run(tmp_path, "t3_first", ["t3", "t2"], ["obedient", "ground-truth"])
    a, b = by_case(first), by_case(second)
    assert a.keys() == b.keys() and len(a) == 2 * 2 * 2  # models x (benign, attack) x topologies
    for case, ra in a.items():
        rb = b[case]
        assert ra.run_id == rb.run_id
        assert (ra.outcome, ra.attack_succeeded) == (rb.outcome, rb.attack_succeeded), case
        assert ra.marker_hits == rb.marker_hits, case
        # The same responses in both orders. The counter is global, so its numbers depend on
        # how many real calls earlier runs made; they are compared within a store below.
        assert unnumbered(ra.messages) == unnumbered(rb.messages), case
        assert unnumbered([e.payload for e in ra.events]) == unnumbered(
            [e.payload for e in rb.events]
        ), case
        if case[2] == "t3":
            for store, runs in ((first, a), (second, b)):
                child, parent = runs[case], runs[(case[0], case[1], "t2")]
                row = store.get_prefix_divergence(child.run_id)
                assert row is not None
                # Up to the divergence, T3 and T2 saw the very same responses, numbers
                # included: one of them made each call and the other replayed it.
                assert agent_messages(child, row.n_shared_calls) == agent_messages(
                    parent, row.n_shared_calls
                ), case
            row_a = first.get_prefix_divergence(ra.run_id)
            row_b = second.get_prefix_divergence(rb.run_id)
            assert row_a is not None and row_a == row_b, case
            parent = a[(case[0], case[1], "t2")]
            assert row_a.parent_run_id == parent.run_id
            # T3 diverges at its first reviewer call, and everything before it was shared.
            first_review = next(
                i for i, c in enumerate(ra.calls) if (c.agent_id or "").startswith("reviewer#")
            )
            assert (row_a.divergence_call_idx, row_a.n_shared_calls) == (first_review,) * 2
        else:
            assert first.get_prefix_divergence(ra.run_id) is None  # t2 has no parent
    first.close()
    second.close()


@pytest.mark.usefixtures("counting")
def test_t5s_agent_calls_hit_t3s_cache_until_the_humans_first_rejection(tmp_path: Path) -> None:
    store = run(tmp_path, "t5", ["t3", "t5", "t5_summary"], ["obedient", "ground-truth"])
    runs = by_case(store)
    for (model, injection, topology), child in runs.items():
        # reviewer_exposed is stored, and recomputes from the stored trace (D63).
        exposed = summarise(child.marker_hits, child.events).reviewer_exposed
        assert child.reviewer_exposed is exposed
        assert exposed is (model == "obedient" and injection is not None)
        if topology == "t3":
            continue
        parent = runs[(model, injection, "t3")]
        row = store.get_prefix_divergence(child.run_id)
        assert row is not None and row.parent_run_id == parent.run_id
        if topology == "t5_summary":
            # The first side call (a summary) is the first call T3 never made.
            first_summary = next(
                e.call_idx for e in child.events if e.kind == "approval_request"
            )
            assert row.divergence_call_idx == first_summary
            continue
        rejections = [e for e in child.events
                      if e.kind == "approval_decision" and e.payload["verdict"] == "reject"]
        if model == "obedient" and injection is not None:
            # The human rejects the attack call; T5's next call (the worker, with the block
            # error) is the first one T3 didn't make.
            assert rejections and child.attack_succeeded is False
            assert parent.attack_succeeded is True
            made_before = 1 + max(
                e.call_idx for e in child.events
                if e.call_idx is not None and e.seq < rejections[0].seq
            )
            assert row.divergence_call_idx == made_before
            assert not child.calls[made_before].cached
        else:
            assert not rejections and row.divergence_call_idx is None
        shared = row.n_shared_calls
        assert [c.request_key for c in child.calls[:shared]] == [
            c.request_key for c in parent.calls[:shared]
        ]
        assert all(c.cached for c in child.calls[:shared])  # T3 ran first and made them
    store.close()
