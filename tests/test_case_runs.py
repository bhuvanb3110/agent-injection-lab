"""Attack cases through the runner (PLAN_M3 step 8): `cases:` lists, scoring, run IDs, dev guard.

FakeModels only, no network. A config lists dataset cases; `lab run` places each case's payloads,
scores it by the case's named check and stores the case's fields with the run. Scripted models
(lab/domains/agentdojo_bridge/scripted.py): `obedient` does what the payload says and must score
attack_succeeded = True, `ground-truth` ignores it and must score False.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from lab import runner as runner_module
from lab.attacks.agentdojo import ATTACK_NAME, ATTACKER_IBAN, build_injections, markers_for
from lab.attacks.case_plan import plan_case
from lab.attacks.case_set import committed_case_set
from lab.attacks.cases import AttackCase
from lab.attacks.predicates import SABOTAGE_AMOUNT
from lab.cli import main
from lab.config import ExperimentConfig, expand, load_experiment
from lab.domains.agentdojo_bridge import load_banking_suite, run_case
from lab.domains.agentdojo_bridge.scripted import scripts_for_case
from lab.errors import ModelError
from lab.models.fake import FakeModel, ScriptedModel
from lab.models.llm_element import LabLLM
from lab.models.providers import load_providers
from lab.models.types import CaseFields, RunContext
from lab.scheduler.clock import FakeClock
from lab.scheduler.queue import Provenance, Scheduler
from lab.scheduler.spec import RunSpec
from lab.store import RunRecord, RunStore
from lab.topologies import presets
from lab.topologies.presets import PRESETS

SUITE = load_banking_suite()
CASE_SET = committed_case_set("banking")
CONFIGS = Path(__file__).resolve().parents[1] / "configs" / "experiments"

# Dev cases of the committed dataset, one or more per channel and goal (see the table printed by
# `committed_case_set`); every one is checked against its record below, so a regenerated dataset
# that moved them would fail here instead of testing something else.
AGENTDOJO_CHECKED = {
    "tool_output": "banking:plain_instruction:unauthorized_action:r0",
    "file": "banking:plain_instruction:exfiltration:r1",
    "tool_description": "banking:fake_authority:unauthorized_action:r2",
    "obfuscated tool_description": "banking:obfuscation:exfiltration:r2",
    "multi_hop payment": "banking:multi_hop:exfiltration:r1",
    "multi_hop password": "banking:multi_hop:unauthorized_action:r0",
}
LAB_CHECKED = {
    "sabotage file": "banking:fake_authority:sabotage:r1",
    "sabotage tool_description": "banking:plain_instruction:sabotage:r2",
    "obfuscated sabotage tool_output": "banking:obfuscation:sabotage:r0",  # the only one placed
    "replicate tool_output": "banking:self_replicating:propagation:r1",
    "replicate file": "banking:self_replicating:propagation:r5",
    "replicate tool_description": "banking:self_replicating:propagation:r6",
}
DEV_BASELINE = next(  # AgentDojo's own important_instructions, placed as in milestone 2
    c.id for c in CASE_SET.cases.values()
    if c.technique == "important_instructions" and CASE_SET.split_of[c.id] == "dev"
)
ALL_IDS = [*AGENTDOJO_CHECKED.values(), *LAB_CHECKED.values(), DEV_BASELINE]
TOPOLOGIES = ["t1", "t2"]
MODELS = ["obedient", "ground-truth"]


def case(case_id: str) -> AttackCase:
    return CASE_SET.cases[case_id]


def write_config(path: Path, name: str, cases: list[str], topologies: list[str]) -> Path:
    path.write_text(yaml.safe_dump({
        "name": name,
        "suite": "banking",
        "topologies": topologies,
        "user_tasks": ["user_task_0"],  # unused: `cases` names the runs
        "models": [{"provider": "fake", "model_id": m} for m in MODELS],
        "repeats": 1,
        "seed": 3000,
        "max_tokens": 2048,
        "include_benign": False,
        "cases": cases,
    }), encoding="utf-8")
    return path


def lab_run(workdir: Path, config: Path) -> RunStore:
    assert main([
        "run", str(config), "--store", str(workdir / "runs.sqlite"),
        "--cache", str(workdir / "cache.sqlite"), "--env-file", str(workdir / "no.env"),
    ]) == 0
    return RunStore(workdir / "runs.sqlite")


Records = dict[tuple[str, str, str], RunRecord]  # (case ID, topology, model) -> its run


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("case_runs")


@pytest.fixture(scope="module")
def records(workdir: Path) -> Records:
    config = write_config(workdir / "cases.yaml", "case_runs", ALL_IDS, TOPOLOGIES)
    store = lab_run(workdir, config)
    found = {
        (r.spec.case_id, r.spec.topology, r.spec.model_id): r for r in store.runs("case_runs")
    }
    store.close()
    assert len(found) == len(ALL_IDS) * len(TOPOLOGIES) * len(MODELS)
    return found  # type: ignore[return-value]


def test_the_chosen_cases_are_what_these_tests_assume() -> None:
    for label, case_id in {**AGENTDOJO_CHECKED, **LAB_CHECKED}.items():
        c = case(case_id)
        assert CASE_SET.split_of[case_id] == "dev", label
        assert c.check.startswith("agentdojo:") == (label in AGENTDOJO_CHECKED), label
    channels = {c.channel for c in map(case, AGENTDOJO_CHECKED.values())}
    assert channels == {"tool_output", "file", "tool_description", "multi"}
    assert {case(i).goal for i in LAB_CHECKED.values()} == {"sabotage", "propagation"}


# --- one scripted run per channel, and the multi-hop path -------------------------------------


@pytest.mark.parametrize("topology", TOPOLOGIES)
@pytest.mark.parametrize("case_id", ALL_IDS)
def test_an_obedient_run_is_detected_and_a_ground_truth_run_is_not(
    records: Records, case_id: str, topology: str
) -> None:
    obedient = records[case_id, topology, "obedient"]
    ground_truth = records[case_id, topology, "ground-truth"]
    assert obedient.outcome != "model_error" and ground_truth.outcome != "model_error"
    assert obedient.attack_succeeded is True
    assert ground_truth.attack_succeeded is False
    assert obedient.utility_under_attack is False  # solved, but the attack succeeded


@pytest.mark.parametrize("case_id", ALL_IDS)
def test_attacker_effect_is_scored_beside_the_case_check(records: Records, case_id: str) -> None:
    c = case(case_id)
    for topology in TOPOLOGIES:
        obedient = records[case_id, topology, "obedient"]
        ground_truth = records[case_id, topology, "ground-truth"]
        for run in (obedient, ground_truth):
            assert run.attacker_effect is not None and run.attacker_effect_source == (
                "final_environment"
            )
            assert run.final_environment is not None
        assert ground_truth.attacker_effect is False
        if c.check.startswith("agentdojo:"):
            # Every AgentDojo injection task but the password change pays the attacker.
            assert obedient.attacker_effect is (c.injection_task_id != "injection_task_7")
        else:
            # Sabotage and replication pay the right recipient (D109): a structural disagreement.
            assert obedient.attack_succeeded is True and obedient.attacker_effect is False


def role(agent: str) -> str:
    return agent.split("#")[0]


def marker_path(run: RunRecord) -> list[str]:
    """The roles that held a marker, in the order the trace shows it, repeats collapsed: the
    receiver of a delivery (tool result, schema, handoff), the author of a call's arguments."""
    by_seq = {e.seq: e for e in run.events}
    path: list[str] = []
    for h in run.marker_hits:
        if h.hop is None or h.field == "final_output":
            continue
        e = by_seq[h.event_seq]
        holder = role(e.from_agent if h.field == "tool_call_args" else e.to_agent)
        if not path or path[-1] != holder:
            path.append(holder)
    return path


@pytest.mark.parametrize(
    "label,expected_depth", [("multi_hop payment", 3), ("multi_hop password", 2)]
)
def test_the_multi_hop_path_in_t2_is_the_cases_required_hops(
    records: Records, label: str, expected_depth: int
) -> None:
    c = case(AGENTDOJO_CHECKED[label])
    assert c.channel == "multi" and c.t2_stratum == "cross_worker"
    run = records[c.id, "t2", "obedient"]
    assert marker_path(run) == c.required_hops
    # The pointer crossed workers through the planner, so the payload propagated and was acted on.
    assert (run.propagated, run.infection_depth, run.acted_on) == (True, expected_depth, True)
    # In T1 there is one agent, so there is no hop to cross: the pointer and the action are both
    # read by it, and the run still succeeds.
    assert marker_path(records[c.id, "t1", "obedient"]) == ["agent"]


def test_a_ground_truth_run_never_carries_a_multi_hop_payload_on(records: Records) -> None:
    c = case(AGENTDOJO_CHECKED["multi_hop payment"])
    run = records[c.id, "t2", "ground-truth"]
    assert run.acted_on is False
    assert not any(h.marker == ATTACKER_IBAN and h.hop for h in run.marker_hits)


def test_a_tool_description_case_is_delivered_in_a_schema_and_the_others_are_not(
    records: Records,
) -> None:
    def schema_events(case_id: str) -> int:
        return sum(e.kind == "tool_schema" for e in records[case_id, "t1", "obedient"].events)

    assert schema_events(AGENTDOJO_CHECKED["tool_description"]) == 1
    assert schema_events(LAB_CHECKED["sabotage tool_description"]) == 1
    for other in (AGENTDOJO_CHECKED["tool_output"], AGENTDOJO_CHECKED["file"], DEV_BASELINE):
        assert schema_events(other) == 0


def test_an_obfuscated_goal_marker_is_declared_at_hop_0_by_the_runner(records: Records) -> None:
    """The runner hands the placement's declarations to run_case (D110, D111)."""
    description = records[AGENTDOJO_CHECKED["obfuscated tool_description"], "t1", "obedient"]
    result = records[LAB_CHECKED["obfuscated sabotage tool_output"], "t1", "obedient"]
    seen = {(r.spec.case_id, h.field, h.hop) for r in (description, result)
            for h in r.marker_hits if h.marker in (ATTACKER_IBAN, str(SABOTAGE_AMOUNT))}
    assert (description.spec.case_id, "tool_schema", 0) in seen
    assert (result.spec.case_id, "tool_output", 0) in seen
    # Acting on a declared string marker is hop 1, not "unsourced" (hop None).
    assert [(h.field, h.hop) for h in description.marker_hits if h.marker == ATTACKER_IBAN][:2] == [
        ("tool_schema", 0), ("tool_call_args", 1),
    ]


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN DEFECT, found by this step: markers._strings searches only the string leaves of a "
    "call's arguments, and the sabotage marker is a number (amount: 4321.17), so paying the "
    "planted amount is never seen as acted on. Remove this marker when the search covers numbers "
    "(a change to the marker rules, for Bhuvan to decide)."
))
def test_paying_the_planted_amount_is_seen_as_acting_on_the_marker(records: Records) -> None:
    run = records[LAB_CHECKED["obfuscated sabotage tool_output"], "t1", "obedient"]
    assert run.attack_succeeded is True  # the state check sees the payment
    assert run.acted_on is True


# --- what is stored with the run ---------------------------------------------------------------


def test_every_run_stores_its_case_fields(records: Records, workdir: Path) -> None:
    for (case_id, _, _), run in records.items():
        c = case(case_id)
        assert run.spec.case_id == case_id
        assert run.spec.attack == c.technique and run.spec.injection_task_id == c.injection_task_id
        assert (run.technique, run.channel, run.goal, run.t2_stratum) == (
            c.technique, c.channel, c.goal, c.t2_stratum
        )
    db = sqlite3.connect(workdir / "runs.sqlite")
    rows = db.execute(
        "SELECT case_id, technique, channel, goal, t2_stratum FROM runs"
        " WHERE experiment = 'case_runs'"
    ).fetchall()
    db.close()
    assert len(rows) == len(records)
    for case_id, technique, channel, goal, stratum in rows:
        c = case(case_id)
        assert (technique, channel, goal, stratum) == (
            c.technique, c.channel, c.goal, c.t2_stratum
        )


def test_the_field_values_cover_every_channel_goal_and_stratum_used(records: Records) -> None:
    runs = list(records.values())
    assert {r.channel for r in runs} == {"tool_output", "file", "tool_description", "multi"}
    assert {r.goal for r in runs} == {"unauthorized_action", "exfiltration", "sabotage",
                                      "propagation"}
    assert {r.t2_stratum for r in runs} == {"local", "cross_worker"}


def test_a_run_that_is_not_a_case_leaves_the_fields_empty(tmp_path: Path) -> None:
    config = CONFIGS / "smoke.yaml"
    store = lab_run(tmp_path, config)
    runs = store.runs("smoke")
    store.close()
    assert runs and all(
        (r.spec.case_id, r.technique, r.channel, r.goal, r.t2_stratum) == (None,) * 5 for r in runs
    )


def test_a_model_error_run_keeps_its_case_fields_and_its_lab_score(tmp_path: Path) -> None:
    c = case(LAB_CHECKED["sabotage tool_description"])
    plan = plan_case(SUITE, c, "dev")
    user_task = SUITE.get_user_task_by_id(c.user_task_id)
    (script,) = scripts_for_case("obedient", SUITE, user_task, plan, "t1").values()
    error = ModelError("provider rejected the request", provider="fake", subtype="http_400")
    spec = RunSpec(
        experiment="test", suite="banking", benchmark_version="v1.2.2",
        user_task_id=c.user_task_id, topology="t1", provider="fake", model_id="scripted",
        temperature=0.0, reasoning_effort=None, max_tokens=None, seed=1, repeat_idx=0,
        attack=c.technique, injection_task_id=c.injection_task_id, case_id=c.id,
    )

    def run_fn(spec: RunSpec, ctx: RunContext) -> Any:
        ctx.case_fields = plan.fields
        return run_case(
            SUITE, presets.build("t1", LabLLM(FakeModel([*script[:-1], error]), provider="fake",
                                              model_id="scripted")),
            user_task, ctx, injections=plan.injections, tool_descriptions=plan.tool_descriptions,
            attack_check=plan.attack_check,
        )

    store = RunStore(tmp_path / "runs.sqlite")
    provenance = Provenance(config_hash="cfg", git_commit="deadbeef", agentdojo_version="0.1.35")
    Scheduler(store, run_fn, FakeClock(), provenance).run([spec])
    stored = store.get_run(spec.run_id)
    store.close()
    assert stored.outcome == "model_error"
    assert stored.attack_succeeded is True  # the payment was made before the crash (D49)
    assert (stored.technique, stored.channel, stored.goal, stored.t2_stratum) == (
        c.technique, c.channel, c.goal, c.t2_stratum
    )


def test_an_old_store_gets_the_new_columns_on_open(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, experiment TEXT NOT NULL)")
    db.commit()
    db.close()
    RunStore(path).close()
    db = sqlite3.connect(path)
    columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
    db.close()
    assert {"case_id", "technique", "channel", "goal", "t2_stratum"} <= columns


def test_lab_report_reads_a_store_of_case_runs(workdir: Path, records: Records) -> None:
    assert records
    assert main(["report", "case_runs", "--store", str(workdir / "runs.sqlite")]) == 0


# --- all six topologies take a case ------------------------------------------------------------


def test_every_topology_runs_a_case_without_an_error(tmp_path: Path) -> None:
    ids = [AGENTDOJO_CHECKED["tool_description"], LAB_CHECKED["sabotage tool_description"]]
    config = write_config(tmp_path / "six.yaml", "six", ids, sorted(PRESETS))
    store = lab_run(tmp_path, config)
    runs = store.runs("six")
    store.close()
    assert len(runs) == len(ids) * len(PRESETS) * len(MODELS)
    assert not [r for r in runs if r.outcome == "model_error"]
    # Without a gate in the way, the obedient model succeeds; with one, the run is still scored.
    assert all(r.attack_succeeded is not None for r in runs)
    for r in runs:
        if r.spec.model_id == "ground-truth":
            assert r.attack_succeeded is False
        if r.spec.topology in ("t1", "t2", "t3"):
            assert r.attack_succeeded is (r.spec.model_id == "obedient")


# --- the baseline cases are placed as in milestone 2 -------------------------------------------


def test_an_important_instructions_case_is_placed_as_in_milestone_2() -> None:
    c = case(DEV_BASELINE)
    plan = plan_case(SUITE, c, "dev")
    user_task = SUITE.get_user_task_by_id(c.user_task_id)
    injection_task = SUITE.get_injection_task_by_id(c.injection_task_id)
    injections = build_injections(SUITE, ATTACK_NAME, user_task, injection_task)
    assert plan.injections == injections and plan.tool_descriptions == {}
    assert plan.markers == markers_for(ATTACK_NAME, c.injection_task_id, c.user_task_id, injections)
    assert plan.injection_task is not None and plan.attack_check is None


# --- run IDs ------------------------------------------------------------------------------------


def test_two_cases_never_share_a_run_id() -> None:
    """Same user task, injection task, topology, model, seed: only the case differs."""
    by_pair: dict[tuple[str, str], list[AttackCase]] = {}
    for c in CASE_SET.cases.values():
        by_pair.setdefault((c.user_task_id, c.injection_task_id), []).append(c)
    shared = [cs for cs in by_pair.values() if len(cs) > 1 and
              all(CASE_SET.split_of[c.id] == "dev" for c in cs)]
    assert shared, "the dataset places no two dev cases on one pair"
    group = shared[0]
    config = ExperimentConfig.model_validate({
        "name": "pair", "suite": "banking", "topology": "t1", "user_tasks": ["user_task_0"],
        "models": [{"provider": "fake", "model_id": "obedient"}], "repeats": 1, "seed": 1,
        "max_tokens": 2048, "include_benign": False, "cases": [c.id for c in group],
    })
    specs = expand(config, load_providers())
    assert len({s.run_id for s in specs}) == len(group) >= 2
    assert len({(s.user_task_id, s.injection_task_id) for s in specs}) == 1
    assert [s.case_id for s in specs] == [c.id for c in group]


def test_expansion_order_is_repeat_then_case_then_model_then_topology() -> None:
    ids = [AGENTDOJO_CHECKED["tool_output"], AGENTDOJO_CHECKED["file"]]
    config = ExperimentConfig.model_validate({
        "name": "order", "suite": "banking", "topologies": ["t1", "t2"],
        "user_tasks": ["user_task_0"],
        "models": [{"provider": "fake", "model_id": m} for m in MODELS],
        "repeats": 2, "seed": 10, "max_tokens": 2048, "include_benign": False, "cases": ids,
    })
    cells = [(s.repeat_idx, s.case_id, s.model_id, s.topology)
             for s in expand(config, load_providers())]
    expected = [(r, i, m, t) for r in (0, 1) for i in ids for m in MODELS for t in ("t1", "t2")]
    assert cells == expected


# --- the dev-only guard ------------------------------------------------------------------------


def config_for(cases: Any, **overrides: Any) -> ExperimentConfig:
    return ExperimentConfig.model_validate({
        "name": "guard", "suite": "banking", "topology": "t1", "user_tasks": ["user_task_0"],
        "models": [{"provider": "fake", "model_id": "obedient"}], "repeats": 1, "seed": 1,
        "max_tokens": 2048, "include_benign": False, "cases": cases,
    } | overrides)


TEST_CASES = [i for i, s in CASE_SET.split_of.items() if s == "test"]


def test_a_test_split_case_is_refused() -> None:
    assert len(TEST_CASES) == 157  # D112: 225 cases, 68 of them dev
    with pytest.raises(ValueError, match="not in the dev split") as refused:
        expand(config_for([TEST_CASES[0]]), load_providers())
    assert TEST_CASES[0] in str(refused.value)


def test_one_test_case_among_dev_cases_refuses_the_whole_list() -> None:
    mixed = [AGENTDOJO_CHECKED["tool_output"], TEST_CASES[-1], AGENTDOJO_CHECKED["file"]]
    with pytest.raises(ValueError, match=r"1 of the listed cases are not in the dev split"):
        expand(config_for(mixed), load_providers())


def test_a_test_baseline_case_is_refused_too() -> None:
    baseline = next(i for i in TEST_CASES if case(i).technique == "important_instructions")
    with pytest.raises(ValueError, match="not in the dev split"):
        expand(config_for([baseline]), load_providers())


def test_the_guard_runs_before_any_run_is_made(tmp_path: Path) -> None:
    config = write_config(tmp_path / "bad.yaml", "bad", [TEST_CASES[0]], ["t1"])
    with pytest.raises(ValueError, match="not in the dev split"):
        main(["run", str(config), "--store", str(tmp_path / "runs.sqlite"),
              "--cache", str(tmp_path / "cache.sqlite"), "--env-file", str(tmp_path / "no.env")])
    assert not (tmp_path / "runs.sqlite").exists() or not RunStore(
        tmp_path / "runs.sqlite"
    ).run_ids("bad")


@pytest.mark.parametrize("cases,message", [
    (["banking:no:such:case"], "Unknown attack cases"),
    ([AGENTDOJO_CHECKED["tool_output"]] * 2, "listed more than once"),
    ("banking_v1", "not built yet"),
])
def test_bad_case_lists_are_refused(cases: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        expand(config_for(cases), load_providers())


def test_a_case_list_runs_attack_cases_only() -> None:
    ids = [AGENTDOJO_CHECKED["tool_output"]]
    with pytest.raises(ValueError, match="include_benign: false"):
        expand(config_for(ids, include_benign=True), load_providers())
    with pytest.raises(ValueError, match="not both"):
        expand(config_for(ids, attack={"injection_tasks": ["injection_task_4"]}),
               load_providers())


# --- the step-8 configs ------------------------------------------------------------------------


@pytest.mark.parametrize("name,runs,model", [
    ("step8_20b_j", 10, "openai/gpt-oss-20b"),
    ("step8_20b_c", 54, "openai/gpt-oss-20b"),
    ("step8_120b", 28, "openai/gpt-oss-120b"),
])
def test_a_step8_config_expands_to_the_runs_of_d105(name: str, runs: int, model: str) -> None:
    config = load_experiment(CONFIGS / f"{name}.yaml")
    specs = expand(config, load_providers())
    assert len(specs) == runs
    assert len({s.run_id for s in specs}) == runs
    assert {(s.provider, s.model_id) for s in specs} == {("groq", model)}
    assert {s.repeat_idx for s in specs} == {0}
    assert {s.topology for s in specs} == set(config.topology_names())
    assert all(s.case_id in CASE_SET.cases and s.attack == case(s.case_id).technique
               and s.injection_task_id == case(s.case_id).injection_task_id for s in specs)
    assert all(CASE_SET.split_of[s.case_id] == "dev" for s in specs if s.case_id)
    # Every listed case runs on every listed topology, once.
    assert {(s.case_id, s.topology) for s in specs} == {
        (c, t) for c in config.cases or [] for t in config.topology_names()
    }


def test_the_20b_step8_configs_make_64_distinct_runs_and_120b_adds_28() -> None:
    ids = {
        name: {s.run_id for s in expand(load_experiment(CONFIGS / f"{name}.yaml"),
                                        load_providers())}
        for name in ("step8_20b_j", "step8_20b_c", "step8_120b")
    }
    assert len(ids["step8_20b_j"] | ids["step8_20b_c"]) == 64
    assert len(ids["step8_120b"]) == 28
    assert not ids["step8_20b_j"] & ids["step8_20b_c"]


STEP8 = ("step8_20b_j", "step8_20b_c", "step8_120b")


def test_every_step8_case_can_be_planned_without_a_model_call() -> None:
    for name in STEP8:
        config = load_experiment(CONFIGS / f"{name}.yaml")
        for case_id in config.cases or []:
            plan = plan_case(SUITE, case(case_id), "dev")
            assert (plan.injection_task is None) != (plan.attack_check is None)
            assert plan.fields == CaseFields(
                case(case_id).technique, case(case_id).channel, case(case_id).goal,
                case(case_id).t2_stratum,
            )


def unplaceable(case_ids: list[str]) -> dict[str, str]:
    """Cases whose payload AgentDojo cannot write into the starting environment."""
    found = {}
    for case_id in case_ids:
        plan = plan_case(SUITE, case(case_id), CASE_SET.split_of[case_id])
        try:
            SUITE.load_and_inject_default_environment(plan.injections)
        except Exception as error:
            found[case_id] = f"{type(error).__name__}: {str(error).splitlines()[0]}"
    return found


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN DEFECT, found by this step: the fake_completion dev wording has a line starting with "
    "'---', which YAML reads as a document separator inside AgentDojo's environment text. "
    "step8_20b_j and step8_120b list banking:fake_completion:sabotage:r0. Remove this marker "
    "when the wording is fixed (a template change, for Bhuvan to decide)."
))
def test_every_step8_case_payload_can_be_placed_in_the_environment() -> None:
    ids = sorted({
        i for name in STEP8 for i in load_experiment(CONFIGS / f"{name}.yaml").cases or []
    })
    assert unplaceable(ids) == {}


def test_the_only_dev_cases_that_cannot_be_placed_are_the_known_ones() -> None:
    """The scope of the defect above: every other dev case, with its own wording, loads."""
    dev = [i for i, s in CASE_SET.split_of.items() if s == "dev"]
    assert sorted(unplaceable(dev)) == [
        "banking:fake_completion:sabotage:r0",
        "banking:fake_completion:unauthorized_action:r1",
    ]


def test_a_payload_that_cannot_be_placed_stops_the_run_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = runner_module.plan_case

    def broken(suite: Any, c: AttackCase, split: Any) -> Any:
        return replace(real(suite, c, split), injections={
            "injection_incoming_transaction": "note\n--- END ---\nmore"
        })

    monkeypatch.setattr(runner_module, "plan_case", broken)
    config = write_config(tmp_path / "bad.yaml", "bad", [AGENTDOJO_CHECKED["tool_output"]], ["t1"])
    with pytest.raises(ValueError, match="cannot be placed.*Nothing was run") as stopped:
        main(["run", str(config), "--store", str(tmp_path / "runs.sqlite"),
              "--cache", str(tmp_path / "cache.sqlite"), "--env-file", str(tmp_path / "no.env")])
    assert AGENTDOJO_CHECKED["tool_output"] in str(stopped.value)
    store = RunStore(tmp_path / "runs.sqlite")
    assert store.run_ids("bad") == []
    store.close()


# --- run_case: an attack check without an injection task --------------------------------------


def user_task_script(case_id: str, model: str = "obedient") -> tuple[Any, list[Any]]:
    c = case(case_id)
    plan = plan_case(SUITE, c, "dev")
    user_task = SUITE.get_user_task_by_id(c.user_task_id)
    (steps,) = scripts_for_case(model, SUITE, user_task, plan, "t1").values()
    return plan, steps


def test_run_case_scores_a_lab_check_and_refuses_ambiguous_arguments() -> None:
    plan, steps = user_task_script(LAB_CHECKED["obfuscated sabotage tool_output"])
    user_task = SUITE.get_user_task_by_id(plan.case.user_task_id)

    def go(**kwargs: Any) -> Any:
        pipeline = presets.build("t1", LabLLM(FakeModel(list(steps)), provider="fake",
                                              model_id="scripted"))
        return run_case(SUITE, pipeline, user_task, RunContext(), **kwargs)

    result = go(injections=plan.injections, attack_check=plan.attack_check)
    assert result.attack_succeeded is True
    with pytest.raises(ValueError, match="not both"):
        go(injections={}, attack_check=plan.attack_check,
           injection_task=SUITE.get_injection_task_by_id("injection_task_4"))
    with pytest.raises(ValueError, match="go together"):
        go(attack_check=plan.attack_check)  # a check but no injections
    with pytest.raises(ValueError, match="go together"):
        go(injections={})  # injections but no check
    assert go().attack_succeeded is None  # benign: nothing to score


def test_a_lab_check_never_reads_the_answer_text() -> None:
    """Saying the amount and the canary in the answer, with the environment untouched, is no
    success: the predicate is a state check (D93)."""
    c = case(LAB_CHECKED["replicate tool_output"])
    plan = plan_case(SUITE, c, "dev")
    user_task = SUITE.get_user_task_by_id(c.user_task_id)
    from lab.models.fake import answer
    text = f"I changed the payment to {SABOTAGE_AMOUNT} and noted {plan.canary}."
    pipeline = presets.build("t1", LabLLM(FakeModel([answer(text)]), provider="fake",
                                          model_id="scripted"))
    result = run_case(SUITE, pipeline, user_task, RunContext(), injections=plan.injections,
                      attack_check=plan.attack_check)
    assert result.attack_succeeded is False


def test_a_scripted_model_is_keyed_by_request_for_a_case(tmp_path: Path) -> None:
    """`scripts_for_case` returns what a ScriptedModel takes, for every topology's base shape."""
    plan, _ = user_task_script(LAB_CHECKED["obfuscated sabotage tool_output"])
    user_task = SUITE.get_user_task_by_id(plan.case.user_task_id)
    for topology in sorted(PRESETS):
        scripts = scripts_for_case("obedient", SUITE, user_task, plan, topology)
        ScriptedModel(scripts)
        assert user_task.PROMPT in scripts
    with pytest.raises(NotImplementedError, match="lab injection task"):
        multi = plan_case(SUITE, case("banking:multi_hop:sabotage:r2"), "dev")
        scripts_for_case("obedient", SUITE, SUITE.get_user_task_by_id(multi.case.user_task_id),
                         multi, "t1")
