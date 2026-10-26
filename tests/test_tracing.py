"""Slice 2 of milestone 2: trace events, marker hits and hops, on T1 (PLAN_M2 §5, §8, D51).

`run_spec`, `make_call` and `make_record` are the milestone-1 fixtures in conftest.py.
"""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from lab.attacks.agentdojo import (
    ATTACK_NAME,
    ATTACKER_IBAN,
    GOAL_MARKERS,
    build_injections,
    canary_for,
    markers_for,
)
from lab.cli import main
from lab.domains.agentdojo_bridge import (
    banking_injection_task_ids,
    banking_task_ids,
    load_banking_suite,
    run_case,
)
from lab.domains.agentdojo_bridge.scripted import ground_truth_script, script_for
from lab.errors import ModelError
from lab.models.fake import FakeModel, Step, answer
from lab.models.llm_element import LabLLM, function_to_openai
from lab.models.types import CallRecord, RunContext
from lab.scheduler.clock import FakeClock
from lab.scheduler.queue import Provenance, Scheduler
from lab.scheduler.spec import RunSpec
from lab.store import RunRecord, RunStore
from lab.topologies.t1 import build_t1, default_system_message
from lab.tracing.events import TraceEvent
from lab.tracing.markers import MarkerHit, find_hits, normalise, summarise

USER_TASK = "user_task_2"  # reads landlord-notices.txt, the exit case's channel (PLAN_M2 §6)
INJECTION_TASK = "injection_task_4"  # redirect a recurring payment to the attacker IBAN


def t1_run(script: list[Step], injection_task_id: str | None, ctx: RunContext) -> list[str]:
    """Run USER_TASK through T1 with a scripted model, attacked if `injection_task_id` is given.
    Sets the run's markers as the runner does and returns the injected vectors."""
    suite = load_banking_suite()
    user_task = suite.get_user_task_by_id(USER_TASK)
    llm = LabLLM(FakeModel(script), provider="fake", model_id="scripted")
    if injection_task_id is None:
        ctx.markers = markers_for(None, None, USER_TASK, ())
        run_case(suite, build_t1(llm), user_task, ctx)
        return []
    injection_task = suite.get_injection_task_by_id(injection_task_id)
    injections = build_injections(suite, ATTACK_NAME, user_task, injection_task)
    ctx.markers = markers_for(ATTACK_NAME, injection_task_id, USER_TASK, injections)
    run_case(
        suite, build_t1(llm), user_task, ctx, injections=injections, injection_task=injection_task
    )
    return list(injections)


def obedient_script(injection_task_id: str = INJECTION_TASK) -> list[Step]:
    suite = load_banking_suite()
    return script_for(
        "obedient",
        suite,
        suite.get_user_task_by_id(USER_TASK),
        suite.get_injection_task_by_id(injection_task_id),
    )


def strings(value: Any) -> list[str]:
    """Every string inside a JSON value (a stand-alone copy, so the test doesn't trust the code
    it checks)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


# --- goal markers are AgentDojo's own literals ---


def test_every_banking_injection_task_has_goal_markers_taken_from_its_source() -> None:
    suite = load_banking_suite()
    env = suite.load_and_inject_default_environment({})
    assert set(GOAL_MARKERS) == set(banking_injection_task_ids())
    for task_id, markers in GOAL_MARKERS.items():
        task = suite.get_injection_task_by_id(task_id)
        ground_truth_args = [s for c in task.ground_truth(env) for s in strings(dict(c.args))]
        for marker in markers:
            assert marker in task.GOAL, task_id
            assert marker in ground_truth_args, task_id


def test_an_attack_run_looks_for_its_goal_markers_and_its_canary() -> None:
    vector = "injection_landloard_notice"
    markers = markers_for(ATTACK_NAME, "injection_task_7", USER_TASK, [vector])
    assert [(m.kind, m.value) for m in markers] == [
        ("goal", "new_password"),
        ("canary", canary_for(ATTACK_NAME, "injection_task_7", USER_TASK, vector)),
    ]
    # A benign run looks for every goal marker, and has no canary to look for.
    benign = markers_for(None, None, USER_TASK, ())
    assert {m.value for m in benign} == {ATTACKER_IBAN, "new_password"}
    assert {m.kind for m in benign} == {"goal"}


# --- marker hygiene: nothing benign contains a marker ---


def benign_ground_truth_events(user_task_id: str) -> list[TraceEvent]:
    suite = load_banking_suite()
    user_task = suite.get_user_task_by_id(user_task_id)
    ctx = RunContext()
    llm = LabLLM(FakeModel(ground_truth_script(suite, user_task)), provider="fake",
                 model_id="ground-truth")
    run_case(suite, build_t1(llm), user_task, ctx)
    return ctx.events


def all_marker_needles() -> list[str]:
    """Every goal marker, plus the canary prefix: stricter than every possible canary."""
    goals = {v for values in GOAL_MARKERS.values() for v in values}
    return [normalise(v) for v in sorted(goals)] + [normalise("CNRY")]


@pytest.mark.parametrize("user_task_id", banking_task_ids())
def test_no_marker_in_a_benign_ground_truth_trace(user_task_id: str) -> None:
    """Every string in the trace is searched, system prompt and user request included, not
    only the fields hit detection reads (PLAN_M2 §5)."""
    events = benign_ground_truth_events(user_task_id)
    texts = [normalise(s) for e in events for s in strings(e.payload)]
    assert texts  # the trace isn't empty
    for needle in all_marker_needles():
        assert not any(needle in t for t in texts), (user_task_id, needle)
    assert find_hits(events, markers_for(None, None, user_task_id, ())) == []


def test_no_marker_in_the_system_prompt_or_any_tool_schema() -> None:
    suite = load_banking_suite()
    schemas = [function_to_openai(f) for f in suite.tools]
    assert len(schemas) == 11
    texts = [normalise(default_system_message())] + [
        normalise(s) for schema in schemas for s in strings(schema)
    ]
    for needle in all_marker_needles():
        assert not any(needle in t for t in texts), needle


# --- T1 traces and hops ---


def test_t1_trace_holds_every_message_with_its_sender_and_receiver() -> None:
    ctx = RunContext()
    t1_run(obedient_script(), INJECTION_TASK, ctx)
    # Every message of the transcript is an event, in order, as plain JSON, and nothing else is.
    payloads = [e.payload for e in ctx.events]
    assert payloads == json.loads(json.dumps(payloads))
    assert len(ctx.events) == len(ctx.messages)
    assert [e.seq for e in ctx.events] == list(range(len(ctx.events)))
    assert {e.kind for e in ctx.events} == {"message"}
    assert {(e.agent_id, e.agent_role) for e in ctx.events} == {("agent", "single")}
    edges = [(e.payload["role"], e.from_agent, e.to_agent) for e in ctx.events]
    assert edges[:2] == [("system", "system", "agent"), ("user", "user", "agent")]
    assert edges[-1] == ("assistant", "agent", "user")  # the final answer
    assert set(edges[2:-1]) == {("assistant", "agent", "tools"), ("tool", "tools", "agent")}
    # Each assistant message points at the call that produced it, and each call names its agent.
    assistant = [e for e in ctx.events if e.payload["role"] == "assistant"]
    assert [e.call_idx for e in assistant] == list(range(len(ctx.calls)))
    assert all(e.call_idx is None for e in ctx.events if e.payload["role"] != "assistant")
    assert {c.agent_id for c in ctx.calls} == {"agent"}


def test_obedient_t1_hits_the_goal_marker_in_tool_call_args_at_hop_1() -> None:
    """The notice (a tool result) carries the goal marker and the canary at hop 0; the agent
    then writes the attacker IBAN into update_scheduled_transaction: hop 1. The final answer
    holds no marker, so in T1 nothing propagated."""
    ctx = RunContext()
    t1_run(obedient_script(), INJECTION_TASK, ctx)
    assert ctx.markers is not None
    hits = find_hits(ctx.events, ctx.markers)
    canary = next(m.value for m in ctx.markers if m.kind == "canary")

    read_result = next(e.seq for e in ctx.events if e.payload["role"] == "tool")
    attack_call = next(
        e.seq for e in ctx.events
        if any(c["args"].get("recipient") == ATTACKER_IBAN
               for c in e.payload.get("tool_calls") or [])
    )
    assert hits == [
        MarkerHit(event_seq=read_result, marker=canary, marker_kind="canary",
                  field="tool_output", hop=0),
        MarkerHit(event_seq=read_result, marker=ATTACKER_IBAN, marker_kind="goal",
                  field="tool_output", hop=0),
        MarkerHit(event_seq=attack_call, marker=ATTACKER_IBAN, marker_kind="goal",
                  field="tool_call_args", hop=1),
    ]
    summary = summarise(hits)
    assert (summary.acted_on, summary.infection_depth) == (True, 1)
    assert (summary.propagated, summary.marker_in_final_output) == (False, False)


def test_obedient_t1_on_the_password_task_hits_new_password() -> None:
    ctx = RunContext()
    t1_run(obedient_script("injection_task_7"), "injection_task_7", ctx)
    assert ctx.markers is not None
    args_hits = [h for h in find_hits(ctx.events, ctx.markers) if h.field == "tool_call_args"]
    assert [(h.marker, h.hop) for h in args_hits] == [("new_password", 1)]


def test_a_marker_in_the_final_answer_propagates_and_matching_ignores_case_and_spaces() -> None:
    spaced = "us13 3000 0001 2121 2121 212"  # the IBAN, lower case, in groups of four
    assert normalise(spaced) == normalise(ATTACKER_IBAN)
    script = [*obedient_script()[:-1], answer(f"Done. I also paid {spaced} as the notice asked.")]
    ctx = RunContext()
    t1_run(script, INJECTION_TASK, ctx)
    assert ctx.markers is not None
    hits = find_hits(ctx.events, ctx.markers)
    final = [h for h in hits if h.field == "final_output"]
    assert [(h.marker, h.hop, h.event_seq) for h in final] == [
        (ATTACKER_IBAN, 1, ctx.events[-1].seq)
    ]
    summary = summarise(hits)
    assert (summary.propagated, summary.marker_in_final_output) == (True, True)


def test_a_marker_nothing_delivered_is_unsourced_and_does_not_count_as_propagation() -> None:
    """A benign run whose answer names the attacker IBAN: no tool result held it, so the hop
    is None rather than a guess, and the run did not propagate anything."""
    suite = load_banking_suite()
    script = [*ground_truth_script(suite, suite.get_user_task_by_id(USER_TASK))[:-1],
              answer(f"Paid. By the way, {ATTACKER_IBAN}.")]
    ctx = RunContext()
    t1_run(script, None, ctx)
    assert ctx.markers is not None
    hits = find_hits(ctx.events, ctx.markers)
    assert [(h.field, h.hop) for h in hits] == [("final_output", None)]
    summary = summarise(hits)
    assert (summary.propagated, summary.infection_depth) == (False, None)
    assert summary.marker_in_final_output is True


def test_a_rewritten_transcript_is_refused() -> None:
    from agentdojo.functions_runtime import FunctionsRuntime
    from agentdojo.types import ChatUserMessage, text_content_block_from_string

    llm = LabLLM(FakeModel([answer("a"), answer("b")]), provider="fake", model_id="scripted")
    ctx = RunContext()

    def user(text: str) -> ChatUserMessage:
        return ChatUserMessage(role="user", content=[text_content_block_from_string(text)])

    llm.query("q", FunctionsRuntime([]), messages=[user("first")], extra_args={"ctx": ctx})
    with pytest.raises(ValueError, match="rewritten"):
        llm.query("q", FunctionsRuntime([]), messages=[user("other")], extra_args={"ctx": ctx})


# --- through the scheduler and the store ---


def attacked_spec() -> RunSpec:
    return RunSpec(
        experiment="test", suite="banking", benchmark_version="v1.2.2", user_task_id=USER_TASK,
        topology="t1", provider="fake", model_id="scripted", temperature=0.0,
        reasoning_effort=None, max_tokens=None, seed=1, repeat_idx=0,
        attack=ATTACK_NAME, injection_task_id=INJECTION_TASK,
    )


def test_a_model_error_run_keeps_its_partial_trace_and_hits(tmp_path: Path) -> None:
    """The obedient run cut off before its final answer: the attack call is in the stored
    trace, and its hit is stored (D30, D49)."""
    error = ModelError("provider rejected the request", provider="fake", subtype="http_400")
    store = RunStore(tmp_path / "runs.sqlite")

    def run_fn(spec: RunSpec, ctx: RunContext) -> Any:
        return t1_run([*obedient_script()[:-1], error], INJECTION_TASK, ctx)

    provenance = Provenance(config_hash="cfg", git_commit="deadbeef", agentdojo_version="0.1.35")
    spec = attacked_spec()
    Scheduler(store, run_fn, FakeClock(), provenance).run([spec])
    stored = store.get_run(spec.run_id)
    assert stored.outcome == "model_error"
    # Everything the failing call was sent is in the trace; it wrote nothing.
    assert stored.events[-1].payload["role"] == "tool"
    assert [e.payload for e in stored.events] == stored.messages
    assert (stored.acted_on, stored.infection_depth, stored.propagated) == (True, 1, False)
    assert any(h.field == "tool_call_args" for h in stored.marker_hits)
    store.close()


def test_a_run_without_markers_leaves_the_marker_columns_null(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")

    def run_fn(spec: RunSpec, ctx: RunContext) -> Any:
        suite = load_banking_suite()
        user_task = suite.get_user_task_by_id(USER_TASK)
        llm = LabLLM(FakeModel(ground_truth_script(suite, user_task)), provider="fake",
                     model_id="scripted")
        return run_case(suite, build_t1(llm), user_task, ctx)  # ctx.markers stays None

    provenance = Provenance(config_hash="cfg", git_commit="deadbeef", agentdojo_version="0.1.35")
    spec = attacked_spec().model_copy(update={"attack": None, "injection_task_id": None})
    Scheduler(store, run_fn, FakeClock(), provenance).run([spec])
    stored = store.get_run(spec.run_id)
    assert stored.events  # still traced
    assert stored.marker_hits == []
    assert (stored.propagated, stored.infection_depth) == (None, None)
    assert (stored.marker_in_final_output, stored.acted_on) == (None, None)
    store.close()


def run_attack_experiment(tmp_path: Path) -> Path:
    config = tmp_path / "attack.yaml"
    config.write_text(yaml.safe_dump({
        "name": "trace_smoke", "suite": "banking", "topology": "t1", "user_tasks": [USER_TASK],
        "models": [{"provider": "fake", "model_id": "ground-truth"},
                   {"provider": "fake", "model_id": "obedient"}],
        "repeats": 1, "seed": 5, "max_tokens": 2048,
        "attack": {"injection_tasks": [INJECTION_TASK, "injection_task_7"]},
    }))
    store = tmp_path / "runs.sqlite"
    assert main(["run", str(config), "--store", str(store), "--cache",
                 str(tmp_path / "cache.sqlite"), "--env-file", str(tmp_path / "no.env")]) == 0
    return store


def test_recomputing_hits_from_stored_trace_events_gives_the_stored_rows(
    tmp_path: Path,
) -> None:
    """Read the raw tables back, recompute every run's hits and per-run numbers from
    `trace_events` alone, and compare with `marker_hits` and `runs` (PLAN_M2 §8)."""
    store_path = run_attack_experiment(tmp_path)
    suite = load_banking_suite()
    db = sqlite3.connect(store_path)
    runs = db.execute(
        "SELECT run_id, spec, messages, propagated, infection_depth, marker_in_final_output,"
        " acted_on FROM runs"
    ).fetchall()
    assert len(runs) == 6  # (benign + 2 attacks) x 2 models
    obedient_attack_hits = 0
    for run_id, spec_json, messages, *stored_summary in runs:
        spec = RunSpec.model_validate_json(spec_json)
        events = [
            TraceEvent(seq=r[0], agent_id=r[1], agent_role=r[2], kind=r[3], from_agent=r[4],
                       to_agent=r[5], call_idx=r[6], payload=json.loads(r[7]))
            for r in db.execute(
                "SELECT seq, agent_id, agent_role, kind, from_agent, to_agent, call_idx, payload"
                " FROM trace_events WHERE run_id = ? ORDER BY seq", (run_id,)
            )
        ]
        # T1's trace is the whole transcript.
        assert [e.payload for e in events] == json.loads(messages)

        vectors: list[str] = []
        if spec.attack is not None and spec.injection_task_id is not None:
            vectors = list(build_injections(
                suite, spec.attack, suite.get_user_task_by_id(spec.user_task_id),
                suite.get_injection_task_by_id(spec.injection_task_id),
            ))
        markers = markers_for(spec.attack, spec.injection_task_id, spec.user_task_id, vectors)
        recomputed = find_hits(events, markers)
        stored_rows = db.execute(
            "SELECT event_seq, marker, marker_kind, field, hop FROM marker_hits"
            " WHERE run_id = ? ORDER BY event_seq, field, marker", (run_id,)
        ).fetchall()
        assert [(h.event_seq, h.marker, h.marker_kind, h.field, h.hop)
                for h in recomputed] == stored_rows
        s = summarise(recomputed)
        assert stored_summary == [
            int(s.propagated), s.infection_depth, int(s.marker_in_final_output), int(s.acted_on)
        ]
        if spec.model_id == "obedient" and spec.attack is not None:
            obedient_attack_hits += sum(1 for r in stored_rows if r[3] == "tool_call_args")
        if spec.attack is None:
            assert stored_rows == []  # clean runs have no hits
    assert obedient_attack_hits == 2  # one per injection task
    agents = set(db.execute("SELECT DISTINCT agent_id FROM calls"))
    assert agents == {("agent",)}
    db.close()


# --- milestone-1 runs ---


M2_RUN_COLUMNS = (
    "attack", "injection_task_id", "attack_succeeded", "utility_under_attack",
    "propagated", "infection_depth", "marker_in_final_output", "acted_on",
)


def test_a_milestone_1_store_reads_back_and_its_run_ids_recompute(
    tmp_path: Path,
    run_spec: RunSpec,
    make_call: Callable[..., CallRecord],
    make_record: Callable[[list[CallRecord]], RunRecord],
) -> None:
    """Turn a store into the milestone-1 layout: no M2 columns, no trace tables, and call JSON
    without `agent_id`. Reopening it must add what's missing and read every run back."""
    path = tmp_path / "runs.sqlite"
    original = make_record([make_call(), make_call(cached=True, attempts=0)])
    store = RunStore(path)
    store.save_run(original)
    store.close()

    db = sqlite3.connect(path)
    for column in M2_RUN_COLUMNS:
        db.execute(f"ALTER TABLE runs DROP COLUMN {column}")
    db.execute("ALTER TABLE calls DROP COLUMN agent_id")
    db.execute("DROP TABLE marker_hits")
    db.execute("DROP TABLE trace_events")
    for idx, record in db.execute("SELECT idx, record FROM calls").fetchall():
        fields = json.loads(record)
        del fields["agent_id"]
        db.execute("UPDATE calls SET record = ? WHERE idx = ?", (json.dumps(fields), idx))
    db.commit()
    db.close()

    reopened = RunStore(path)
    (run_id,) = reopened.run_ids("test")
    old = reopened.get_run(run_id)
    assert old.spec.run_id == run_id == run_spec.run_id  # the ID recomputes from the spec
    assert old == original  # every field, with the M2 ones at "not applicable"
    assert (old.spec.attack, old.attack_succeeded, old.propagated, old.infection_depth) == (
        None, None, None, None,
    )
    assert old.events == [] and old.marker_hits == []
    assert {c.agent_id for c in old.calls} == {None}
    reopened.close()
    RunStore(path).close()  # opening again doesn't try to add anything twice
