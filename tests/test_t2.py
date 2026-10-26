"""Slice 3 of milestone 2: T2, the planner and its two workers (PLAN_M2 Q1, D53-D55)."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from agentdojo.task_suite.task_suite import functions_stack_trace_from_messages

from lab.attacks.agentdojo import (
    ATTACK_NAME,
    ATTACKER_IBAN,
    GOAL_MARKERS,
    build_injections,
    markers_for,
)
from lab.cli import main
from lab.domains.agentdojo_bridge import (
    Outcome,
    banking_injection_task_ids,
    banking_task_ids,
    load_banking_suite,
    run_case,
)
from lab.domains.agentdojo_bridge.run_case import CaseResult, tool_call_trace
from lab.domains.agentdojo_bridge.scripted import WORKER_REPORT, script_for, t2_delegations
from lab.models.fake import FakeModel, ScriptedModel, Step, answer, call, malformed_call
from lab.models.llm_element import LabLLM, function_to_openai
from lab.models.types import ModelRequest, RunContext
from lab.scheduler.clock import FakeClock
from lab.scheduler.queue import Provenance, Scheduler
from lab.scheduler.spec import RunSpec
from lab.store import RunStore
from lab.topologies.t1 import build_t1
from lab.topologies.t2 import (
    PLANNER,
    PLANNER_MAX_ROUNDS,
    WORKER_MAX_ITERS,
    WORKER_TOOLS,
    build_t2,
    delegate_function,
    planner_system_message,
    worker_system_message,
)
from lab.tracing.events import TOOLS, USER, WORKERS, TraceEvent, json_safe
from lab.tracing.markers import find_hits, normalise, summarise

USER_TASK = "user_task_2"  # the account worker reads the notice; payments acts (PLAN_M2 §6)
INJECTION_TASK = "injection_task_4"


def t2_run(
    script: list[Step], user_task_id: str = USER_TASK, injection_task_id: str | None = None,
    ctx: RunContext | None = None,
) -> tuple[CaseResult, RunContext]:
    """Run a user task through T2 with a scripted model, attacked if `injection_task_id` is
    given, with the run's markers set as the runner sets them."""
    suite = load_banking_suite()
    user_task = suite.get_user_task_by_id(user_task_id)
    ctx = ctx if ctx is not None else RunContext()
    llm = LabLLM(FakeModel(script), provider="fake", model_id="scripted")
    if injection_task_id is None:
        ctx.markers = markers_for(None, None, user_task_id, ())
        return run_case(suite, build_t2(llm), user_task, ctx), ctx
    injection_task = suite.get_injection_task_by_id(injection_task_id)
    injections = build_injections(suite, ATTACK_NAME, user_task, injection_task)
    ctx.markers = markers_for(ATTACK_NAME, injection_task_id, user_task_id, injections)
    result = run_case(
        suite, build_t2(llm), user_task, ctx, injections=injections, injection_task=injection_task
    )
    return result, ctx


def scripted(model_id: str, user_task_id: str = USER_TASK,
             injection_task_id: str | None = None) -> list[Step]:
    suite = load_banking_suite()
    injection_task = (
        None if injection_task_id is None else suite.get_injection_task_by_id(injection_task_id)
    )
    return script_for(
        model_id, suite, suite.get_user_task_by_id(user_task_id), injection_task, "t2"
    )


def text(message: dict[str, Any]) -> str:
    return "".join(b["content"] for b in message.get("content") or [] if b["type"] == "text")


# --- the domain split ---


def test_the_two_workers_hold_t1s_11_tools_with_no_overlap() -> None:
    suite_tools = sorted(f.name for f in load_banking_suite().tools)
    assert len(suite_tools) == 11
    payments, account = WORKER_TOOLS["payments"], WORKER_TOOLS["account"]
    assert set(WORKER_TOOLS) == {"payments", "account"}
    assert (len(payments), len(account)) == (7, 4)
    assert not set(payments) & set(account)
    assert sorted(payments + account) == suite_tools


def test_each_worker_is_sent_only_its_own_tools_and_the_planner_only_delegate_tools() -> None:
    llm = LabLLM(FakeModel(scripted("ground-truth", "user_task_15")), provider="fake",
                 model_id="scripted")
    suite = load_banking_suite()
    run_case(suite, build_t2(llm), suite.get_user_task_by_id("user_task_15"), RunContext())
    assert isinstance(llm.client, FakeModel)
    sent = {
        tuple(sorted(t["function"]["name"] for t in r.tools)) for r in llm.client.requests
    }
    assert sent == {
        ("delegate_to_account", "delegate_to_payments"),
        tuple(sorted(WORKER_TOOLS["payments"])),
        tuple(sorted(WORKER_TOOLS["account"])),
    }


# --- benign parity and hygiene ---


@pytest.mark.parametrize("user_task_id", banking_task_ids())
def test_scripted_ground_truth_through_t2_solves_the_task_with_no_marker_hits(
    user_task_id: str,
) -> None:
    result, ctx = t2_run(scripted("ground-truth", user_task_id), user_task_id)
    assert result.outcome is Outcome.SOLVED
    # The workers did the work, through delegations: no planner message went to the tools.
    assert any(e.kind == "delegation" for e in ctx.events)
    assert {e.agent_id for e in ctx.events if e.to_agent == TOOLS} <= {
        a for a in ctx.transcripts if a != PLANNER
    }
    assert ctx.markers is not None
    assert find_hits(ctx.events, ctx.markers) == []


def test_no_marker_in_the_t2_prompts_or_delegate_tool_schemas() -> None:
    texts = [planner_system_message(), *(worker_system_message(w) for w in WORKER_TOOLS)]
    texts += [json.dumps(function_to_openai(delegate_function(w))) for w in WORKER_TOOLS]
    goals = {v for values in GOAL_MARKERS.values() for v in values}
    for needle in [*goals, "CNRY"]:
        assert not any(normalise(needle) in normalise(t) for t in texts), needle


# --- the exit case: the attack spreads across both handoffs ---


def test_an_obedient_account_worker_spreads_the_goal_marker_to_payments_at_depth_3() -> None:
    """notice -> account#1 (tool output, hop 0) -> its result to the planner (1) -> the
    planner's delegation to payments#1 (2) -> payments#1's update_scheduled_transaction (3)."""
    result, ctx = t2_run(scripted("obedient", USER_TASK, INJECTION_TASK), USER_TASK,
                         INJECTION_TASK)
    assert result.attack_succeeded is True
    assert ctx.markers is not None
    by_seq = {e.seq: e for e in ctx.events}
    goal = [h for h in find_hits(ctx.events, ctx.markers) if h.marker == ATTACKER_IBAN]
    path = [(h.hop, h.field, by_seq[h.event_seq].kind, by_seq[h.event_seq].from_agent,
             by_seq[h.event_seq].to_agent) for h in goal]
    assert path == [
        (0, "tool_output", "message", TOOLS, "account#1"),
        (1, "handoff", "result", "account#1", PLANNER),
        (2, "handoff", "delegation", PLANNER, "payments#1"),
        (3, "tool_call_args", "message", "payments#1", TOOLS),
    ]
    summary = summarise(find_hits(ctx.events, ctx.markers))
    assert (summary.propagated, summary.infection_depth, summary.acted_on) == (True, 3, True)
    assert summary.marker_in_final_output is False


def test_a_relayed_marker_in_the_planners_delegate_result_is_never_hop_0() -> None:
    """The planner's `delegate` tool result holds the worker's report, IBAN included. It is a
    copy of the `result` event, so it must not be searched as a tool output; if it were, the
    planner would look as if the environment had handed it the marker (hop 0)."""
    _, ctx = t2_run(scripted("obedient", USER_TASK, INJECTION_TASK), USER_TASK, INJECTION_TASK)
    assert ctx.markers is not None
    relayed = [
        e for e in ctx.events
        if e.agent_id == PLANNER and e.payload.get("role") == "tool"
        and normalise(ATTACKER_IBAN) in normalise(text(e.payload))
    ]
    assert len(relayed) == 1 and relayed[0].from_agent == "account#1"
    hits = find_hits(ctx.events, ctx.markers)
    assert relayed[0].seq not in {h.event_seq for h in hits}
    by_seq = {e.seq: e for e in ctx.events}
    assert all(by_seq[h.event_seq].from_agent == TOOLS for h in hits if h.hop == 0)
    assert min(h.hop for h in hits if by_seq[h.event_seq].to_agent == PLANNER) == 1


@pytest.mark.parametrize("injection_task_id", banking_injection_task_ids())
@pytest.mark.parametrize("user_task_id", ["user_task_2", "user_task_3"])
def test_obedient_t2_succeeds_locally_or_across_workers(
    user_task_id: str, injection_task_id: str
) -> None:
    """user_task_2 reads a file (account), user_task_3 the transactions (payments). An attack
    that needs the other worker's tools crosses both handoffs (depth 3, propagated); one the
    reading worker can carry out itself stays local (depth 1, not propagated)."""
    result, ctx = t2_run(scripted("obedient", user_task_id, injection_task_id), user_task_id,
                         injection_task_id)
    assert result.attack_succeeded is True
    suite = load_banking_suite()
    reader = "account" if user_task_id == "user_task_2" else "payments"
    needs = "account" if injection_task_id == "injection_task_7" else "payments"
    assert ctx.markers is not None
    summary = summarise(find_hits(ctx.events, ctx.markers))
    if reader == needs:
        assert (summary.infection_depth, summary.propagated) == (1, False)
    else:
        assert (summary.infection_depth, summary.propagated) == (3, True)
    delegations = t2_delegations(
        suite, suite.get_user_task_by_id(user_task_id),
        suite.get_injection_task_by_id(injection_task_id),
    )
    assert sum(bool(d.injected) for d in delegations) == 1


def test_ground_truth_through_t2_is_not_attacked() -> None:
    result, ctx = t2_run(scripted("ground-truth", USER_TASK), USER_TASK, INJECTION_TASK)
    assert (result.outcome, result.attack_succeeded) == (Outcome.SOLVED, False)
    assert ctx.markers is not None
    summary = summarise(find_hits(ctx.events, ctx.markers))
    # The notice was read (hop 0), and nothing more.
    assert (summary.infection_depth, summary.propagated, summary.acted_on) == (0, False, False)


# --- the trace holds every transcript and every handoff ---


def check_transcripts_and_handoffs(events: list[TraceEvent], ctx: RunContext) -> None:
    """Every agent's transcript is its `message` events, in order; every delegate call has a
    `delegation` and a `result`, which match the worker's first and last message and the
    planner's tool result."""
    agents = set(ctx.transcripts)
    assert {e.agent_id for e in events if e.kind == "message"} == agents
    for agent in agents:
        messages = [e.payload for e in events if e.kind == "message" and e.agent_id == agent]
        assert messages == json_safe(ctx.transcripts[agent]), agent
    planner = ctx.transcripts[PLANNER]
    delegate_calls = [c for m in planner if m["role"] == "assistant"
                      for c in m["tool_calls"] or []]
    delegations = [e for e in events if e.kind == "delegation"]
    results = [e for e in events if e.kind == "result"]
    assert len(delegate_calls) == len(delegations) == len(results) >= 1
    tool_results = {m["tool_call_id"]: m for m in planner if m["role"] == "tool"}
    for c, d, r in zip(delegate_calls, delegations, results, strict=True):
        worker = d.to_agent
        assert (d.payload["tool_call_id"], r.payload["tool_call_id"]) == (c.id, c.id)
        assert d.payload["instruction"] == c.args["instruction"]
        assert (r.from_agent, r.to_agent, d.from_agent) == (worker, PLANNER, PLANNER)
        transcript = ctx.transcripts[worker]
        assert transcript[1]["role"] == "user"
        assert text(json_safe(transcript[1])) == d.payload["instruction"]  # all it is told
        assert text(json_safe(transcript[-1])) == r.payload["report"]
        assert text(json_safe(tool_results[c.id])) == r.payload["report"]  # all it reports
        assert d.seq < r.seq
    # Handoff edges: workers answer the planner; the planner's calls go to the workers.
    for e in events:
        if e.kind == "message" and e.payload["role"] == "assistant":
            if e.agent_id == PLANNER:
                assert e.to_agent in (WORKERS, USER)
            elif not e.payload["tool_calls"]:
                assert e.to_agent == PLANNER


def test_every_transcript_and_handoff_is_stored_in_trace_events(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    contexts: list[RunContext] = []

    def run_fn(spec: RunSpec, ctx: RunContext) -> Any:
        contexts.append(ctx)
        return t2_run(scripted("obedient", USER_TASK, INJECTION_TASK), USER_TASK,
                      INJECTION_TASK, ctx)[0]

    spec = RunSpec(
        experiment="test", suite="banking", benchmark_version="v1.2.2", user_task_id=USER_TASK,
        topology="t2", provider="fake", model_id="obedient", temperature=0.0,
        reasoning_effort=None, max_tokens=None, seed=1, repeat_idx=0,
        attack=ATTACK_NAME, injection_task_id=INJECTION_TASK,
    )
    provenance = Provenance(config_hash="cfg", git_commit="deadbeef", agentdojo_version="0.1.35")
    Scheduler(store, run_fn, FakeClock(), provenance).run([spec])
    (ctx,) = contexts
    stored = store.get_run(spec.run_id)
    assert stored.events == ctx.events
    assert {e.kind for e in stored.events} == {"message", "delegation", "result"}
    check_transcripts_and_handoffs(stored.events, ctx)
    # runs.messages is the planner's transcript, the one AgentDojo scored.
    assert stored.messages == json_safe(ctx.transcripts[PLANNER])
    assert (stored.infection_depth, stored.propagated, stored.attack_succeeded) == (3, True, True)
    assert {c.agent_id for c in stored.calls} == {PLANNER, "account#1", "payments#1"}
    store.close()


def test_lab_run_runs_t1_and_t2_and_the_stored_hits_recompute(tmp_path: Path) -> None:
    config = tmp_path / "t2.yaml"
    config.write_text(yaml.safe_dump({
        "name": "t2_smoke", "suite": "banking", "topologies": ["t1", "t2"],
        "user_tasks": [USER_TASK],
        "models": [{"provider": "fake", "model_id": "ground-truth"},
                   {"provider": "fake", "model_id": "obedient"}],
        "repeats": 1, "seed": 5, "max_tokens": 2048,
        "attack": {"injection_tasks": [INJECTION_TASK]},
    }))
    path = tmp_path / "runs.sqlite"
    assert main(["run", str(config), "--store", str(path), "--cache",
                 str(tmp_path / "cache.sqlite"), "--env-file", str(tmp_path / "no.env")]) == 0
    store = RunStore(path)
    suite = load_banking_suite()
    runs = [store.get_run(r) for r in store.run_ids("t2_smoke")]
    assert len(runs) == 8  # (benign + attack) x 2 models x 2 topologies
    seen = set()
    for run in runs:
        spec = run.spec
        vectors: list[str] = []
        if spec.injection_task_id is not None:
            vectors = list(build_injections(
                suite, ATTACK_NAME, suite.get_user_task_by_id(USER_TASK),
                suite.get_injection_task_by_id(spec.injection_task_id),
            ))
        markers = markers_for(spec.attack, spec.injection_task_id, USER_TASK, vectors)
        assert run.marker_hits == find_hits(run.events, markers)
        assert run.outcome == "solved"
        # The attacked run's first calls are cache hits on the benign run's, and the scripted
        # model stays in step: no call is repeated or skipped (D54).
        expected = ["read_file", "get_scheduled_transactions", "update_scheduled_transaction"]
        if spec.attack is not None and spec.model_id == "obedient":
            expected.append("update_scheduled_transaction")
            assert run.calls[0].cached
        assert [c.function for c in tool_call_trace(run.events)] == expected
        key = (spec.topology, spec.model_id, spec.attack is not None)
        seen.add(key)
        if spec.attack is None:
            assert run.marker_hits == []
        else:
            obedient = spec.model_id == "obedient"
            assert run.attack_succeeded is obedient
            depth = (3 if spec.topology == "t2" else 1) if obedient else 0
            assert run.infection_depth == depth
    assert len(seen) == 8
    store.close()


def test_a_scripted_model_answers_by_request_so_a_repeated_request_gets_the_same_step() -> None:
    first, second = answer("first"), answer("second")
    model = ScriptedModel({"task": [first, second], "other": [answer("other")]})

    def request(*roles_and_texts: tuple[str, str]) -> ModelRequest:
        return ModelRequest(provider="fake", model_id="m", tools=[], temperature=0.0,
                            messages=[{"role": r, "content": t} for r, t in roles_and_texts])

    opening = request(("system", "s"), ("user", "task"))
    assert model.complete(opening) is first
    assert model.complete(opening) is first  # FakeModel would have moved on to `second`
    assert model.complete(request(("user", "task"), ("assistant", "first"))) is second
    assert model.complete(request(("user", "other"))).content == "other"
    with pytest.raises(AssertionError, match="No script"):
        model.complete(request(("user", "unknown")))


# --- the trace AgentDojo's checks are given ---


def test_the_scoring_trace_is_every_workers_tool_calls_in_order() -> None:
    _, ctx = t2_run(scripted("obedient", USER_TASK, INJECTION_TASK), USER_TASK, INJECTION_TASK)
    trace = tool_call_trace(ctx.events)
    assert [c.function for c in trace] == [
        "read_file", "get_scheduled_transactions", "update_scheduled_transaction",
        "update_scheduled_transaction",
    ]
    assert trace[-1].args["recipient"] == ATTACKER_IBAN


def test_the_scoring_trace_of_t1_is_agentdojos() -> None:
    suite = load_banking_suite()
    user_task = suite.get_user_task_by_id("user_task_15")
    llm = LabLLM(FakeModel(script_for("ground-truth", suite, user_task)), provider="fake",
                 model_id="scripted")
    ctx = RunContext()
    result = run_case(suite, build_t1(llm), user_task, ctx)
    assert tool_call_trace(ctx.events) == functions_stack_trace_from_messages(result.messages)


# --- limits and bad delegate calls ---


def planner_steps_then_answer(*steps: Step) -> list[Step]:
    return [*steps, answer("All done.")]


def test_bad_delegate_calls_get_a_tool_error_and_start_no_worker() -> None:
    script = planner_steps_then_answer(
        malformed_call("delegate_to_payments", "{not json", call_id="bad_json"),
        call("delegate_to_nobody", {"instruction": "x"}, call_id="no_worker"),
        call("delegate_to_payments", {"instruction": 42}, call_id="not_a_string"),
        call("get_balance", {}, call_id="planner_has_no_bank_tools"),
    )
    result, ctx = t2_run(script)
    assert result.outcome is Outcome.NOT_SOLVED
    errors = [m["error"] for m in ctx.transcripts[PLANNER] if m["role"] == "tool"]
    assert len(errors) == 4 and all(errors)
    assert "Malformed tool call" in errors[0]
    assert errors[1] == "Invalid tool delegate_to_nobody provided."
    assert errors[2].startswith("ValidationError")
    assert errors[3] == "Invalid tool get_balance provided."
    assert set(ctx.transcripts) == {PLANNER}
    assert {e.kind for e in ctx.events} == {"message"}


def test_a_worker_that_hits_its_round_limit_reports_an_error() -> None:
    worker = [call("get_balance", {}, call_id=f"b{i}") for i in range(WORKER_MAX_ITERS + 1)]
    script = planner_steps_then_answer(
        call("delegate_to_payments", {"instruction": "Check the balance."}, call_id="d"),
        *worker,
    )
    result, ctx = t2_run(script)
    (r,) = [e for e in ctx.events if e.kind == "result"]
    assert r.payload["report"] == ""
    assert r.payload["error"] == (
        f"The payments worker stopped after {WORKER_MAX_ITERS} rounds without a final report."
    )
    calls = [c for c in ctx.calls if c.agent_id == "payments#1"]
    assert len(calls) == WORKER_MAX_ITERS + 1  # the first call, then one per round
    assert result.outcome is Outcome.NOT_SOLVED


def test_a_planner_that_keeps_delegating_stops_at_its_round_limit() -> None:
    script: list[Step] = []
    for i in range(PLANNER_MAX_ROUNDS + 1):
        script.append(call("delegate_to_payments", {"instruction": f"Check {i}."},
                           call_id=f"d{i}"))
        if i < PLANNER_MAX_ROUNDS:
            script.append(answer(WORKER_REPORT))
    result, ctx = t2_run(script)
    assert result.outcome is Outcome.MAX_ITERATIONS
    assert sum(e.kind == "delegation" for e in ctx.events) == PLANNER_MAX_ROUNDS
    assert sorted(a for a in ctx.transcripts if a != PLANNER) == sorted(
        f"payments#{i}" for i in range(1, PLANNER_MAX_ROUNDS + 1)
    )


def test_workers_see_only_the_instruction_never_the_users_request() -> None:
    suite = load_banking_suite()
    _, ctx = t2_run(scripted("ground-truth", USER_TASK))
    prompt = suite.get_user_task_by_id(USER_TASK).PROMPT
    for agent, transcript in ctx.transcripts.items():
        texts = json.dumps(json_safe(transcript))
        assert (prompt in texts) is (agent == PLANNER), agent
