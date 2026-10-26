"""Attack cases through T1: run_case scoring, the scheduler and store, config expansion, the CLI.

Scripted models (lab/domains/agentdojo_bridge/scripted.py): `obedient` does what the injected
text says, `ground-truth` ignores it. So on every attack case the first must score
attack_succeeded = True and the second False.
"""

import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from lab.attacks.agentdojo import ATTACK_NAME, build_injections
from lab.cli import main
from lab.config import ExperimentConfig, expand
from lab.domains.agentdojo_bridge import (
    CaseResult,
    Outcome,
    banking_injection_task_ids,
    load_banking_suite,
    run_case,
)
from lab.domains.agentdojo_bridge.scripted import ground_truth_script, script_for
from lab.errors import ModelError
from lab.models.fake import FakeModel, Step
from lab.models.llm_element import LabLLM
from lab.models.providers import load_providers
from lab.models.types import RunContext
from lab.scheduler.clock import FakeClock
from lab.scheduler.queue import Provenance, Scheduler
from lab.scheduler.spec import RunSpec
from lab.store import RunStore
from lab.topologies.t1 import build_t1

USER_TASK = "user_task_2"  # reads landlord-notices.txt, the exit case's channel (PLAN_M2 §6)
INJECTION_TASKS = banking_injection_task_ids()


def attack_case(
    script: list[Step], injection_task_id: str, ctx: RunContext | None = None
) -> tuple[CaseResult, FakeModel]:
    suite = load_banking_suite()
    user_task = suite.get_user_task_by_id(USER_TASK)
    injection_task = suite.get_injection_task_by_id(injection_task_id)
    model = FakeModel(script)
    result = run_case(
        suite,
        build_t1(LabLLM(model, provider="fake", model_id="scripted")),
        user_task,
        ctx,
        injections=build_injections(suite, ATTACK_NAME, user_task, injection_task),
        injection_task=injection_task,
    )
    return result, model


def scripted(model_id: str, injection_task_id: str) -> list[Step]:
    suite = load_banking_suite()
    return script_for(
        model_id,
        suite,
        suite.get_user_task_by_id(USER_TASK),
        suite.get_injection_task_by_id(injection_task_id),
    )


def test_there_are_nine_banking_injection_tasks() -> None:
    assert [f"injection_task_{i}" for i in range(9)] == INJECTION_TASKS


@pytest.mark.parametrize("injection_task_id", INJECTION_TASKS)
def test_obedient_model_gives_asr_1(injection_task_id: str) -> None:
    result, _ = attack_case(scripted("obedient", injection_task_id), injection_task_id)
    assert result.attack_succeeded is True


@pytest.mark.parametrize("injection_task_id", INJECTION_TASKS)
def test_ground_truth_model_gives_asr_0(injection_task_id: str) -> None:
    result, _ = attack_case(scripted("ground-truth", injection_task_id), injection_task_id)
    assert result.attack_succeeded is False
    assert result.outcome is Outcome.SOLVED  # the user task is unaffected by the payload


def test_payload_is_in_what_the_model_reads() -> None:
    """The obedient script reads the notice first. Its tool result, as the model sees it in the
    next request, must hold both the injected goal and the canary."""
    _, model = attack_case(scripted("obedient", "injection_task_4"), "injection_task_4")
    suite = load_banking_suite()
    goal = suite.get_injection_task_by_id("injection_task_4").GOAL
    read_result = json.dumps(model.requests[1].messages[-1])
    assert goal in read_result and "CNRY" in read_result


def test_benign_run_has_no_attack_score() -> None:
    suite = load_banking_suite()
    user_task = suite.get_user_task_by_id(USER_TASK)
    model = FakeModel(ground_truth_script(suite, user_task))
    result = run_case(
        suite, build_t1(LabLLM(model, provider="fake", model_id="scripted")), user_task
    )
    assert result.attack_succeeded is None


def test_injections_and_injection_task_go_together() -> None:
    suite = load_banking_suite()
    pipeline = build_t1(LabLLM(FakeModel([]), provider="fake", model_id="scripted"))
    task = suite.get_user_task_by_id(USER_TASK)
    with pytest.raises(ValueError, match="go together"):
        run_case(suite, pipeline, task, injections={"injection_landloard_notice": "x"})
    with pytest.raises(ValueError, match="go together"):
        injection_task = suite.get_injection_task_by_id("injection_task_4")
        run_case(suite, pipeline, task, injection_task=injection_task)


def crash_after_attack(injection_task_id: str) -> list[Step]:
    """The obedient script cut off just before its final answer by a ModelError."""
    error = ModelError("provider rejected the request", provider="fake", subtype="http_400")
    return [*scripted("obedient", injection_task_id)[:-1], error]


def test_attack_is_scored_when_the_run_ends_in_a_model_error() -> None:
    """The transfer happened before the crash, so it counts (D49). Tools change the
    environment in place, so run_case can score it although the pipeline never returned."""
    ctx = RunContext()
    with pytest.raises(ModelError):
        attack_case(crash_after_attack("injection_task_4"), "injection_task_4", ctx)
    assert ctx.attack_succeeded is True


def test_a_model_error_before_any_action_is_not_an_attack_success() -> None:
    ctx = RunContext()
    error = ModelError("provider rejected the request", provider="fake", subtype="http_400")
    with pytest.raises(ModelError):
        attack_case([error], "injection_task_4", ctx)
    assert ctx.attack_succeeded is False


def attacked_spec() -> RunSpec:
    return RunSpec(
        experiment="test", suite="banking", benchmark_version="v1.2.2", user_task_id=USER_TASK,
        topology="t1", provider="fake", model_id="scripted", temperature=0.0,
        reasoning_effort=None, max_tokens=None, seed=1, repeat_idx=0,
        attack=ATTACK_NAME, injection_task_id="injection_task_4",
    )


def test_model_error_run_is_stored_with_its_attack_score(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")

    def run_fn(spec: RunSpec, ctx: RunContext) -> CaseResult:
        return attack_case(crash_after_attack("injection_task_4"), "injection_task_4", ctx)[0]

    provenance = Provenance(config_hash="cfg", git_commit="deadbeef", agentdojo_version="0.1.35")
    spec = attacked_spec()
    Scheduler(store, run_fn, FakeClock(), provenance).run([spec])
    stored = store.get_run(spec.run_id)
    assert stored.outcome == "model_error"
    assert stored.attack_succeeded is True
    assert stored.utility_under_attack is False
    assert (stored.spec.attack, stored.spec.injection_task_id) == (ATTACK_NAME, "injection_task_4")
    store.close()


# --- config expansion ---


def attack_config(**overrides: object) -> ExperimentConfig:
    fields: dict[str, object] = {
        "name": "t", "suite": "banking", "topology": "t1", "user_tasks": [USER_TASK, "user_task_0"],
        "models": [{"provider": "fake", "model_id": "ground-truth"},
                   {"provider": "fake", "model_id": "obedient"}],
        "repeats": 2, "seed": 7, "max_tokens": 2048,
        "attack": {"injection_tasks": ["injection_task_4", "injection_task_7"]},
    }
    return ExperimentConfig.model_validate(fields | overrides)


def test_expansion_runs_the_benign_control_then_each_attack_per_user_task() -> None:
    specs = expand(attack_config(), load_providers())
    assert len(specs) == 2 * 2 * 3 * 2  # repeats x user tasks x (benign + 2 attacks) x models
    assert len({s.run_id for s in specs}) == len(specs)
    first_repeat = [s for s in specs if s.repeat_idx == 0]
    # Repeat, then case; the two models sit next to each other in each case.
    assert [(s.user_task_id, s.injection_task_id) for s in first_repeat[::2]] == [
        (USER_TASK, None), (USER_TASK, "injection_task_4"), (USER_TASK, "injection_task_7"),
        ("user_task_0", None), ("user_task_0", "injection_task_4"),
        ("user_task_0", "injection_task_7"),
    ]
    assert [s.model_id for s in first_repeat[:2]] == ["ground-truth", "obedient"]
    assert {s.attack for s in specs if s.injection_task_id} == {ATTACK_NAME}


def test_include_benign_false_leaves_only_attacks() -> None:
    specs = expand(attack_config(include_benign=False), load_providers())
    assert len(specs) == 2 * 2 * 2 * 2
    assert all(s.injection_task_id is not None for s in specs)


def test_all_injection_tasks() -> None:
    specs = expand(
        attack_config(attack={"injection_tasks": "all"}, user_tasks=[USER_TASK], repeats=1),
        load_providers(),
    )
    assert len(specs) == (1 + 9) * 2


def test_config_mistakes_fail_loudly() -> None:
    with pytest.raises(ValueError, match="Unknown banking injection tasks"):
        expand(attack_config(attack={"injection_tasks": ["injection_task_99"]}), load_providers())
    with pytest.raises(ValidationError, match="exactly one"):
        attack_config(topologies=["t1"])
    with pytest.raises(ValidationError, match="exactly one"):
        attack_config(topology=None)
    with pytest.raises(ValidationError, match="nothing to run"):
        attack_config(attack=None, include_benign=False)
    with pytest.raises(ValidationError):
        attack_config(attack={"name": "tool_knowledge", "injection_tasks": "all"})


def test_a_config_with_an_attack_hashes_differently() -> None:
    assert attack_config().config_hash != attack_config(attack=None).config_hash


# --- end to end through the CLI ---


def test_cli_run_stores_attack_scores_and_report_keeps_them_out_of_benign_utility(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "attack.yaml"
    config.write_text(yaml.safe_dump({
        "name": "attack_smoke", "suite": "banking", "topology": "t1", "user_tasks": [USER_TASK],
        "models": [{"provider": "fake", "model_id": "ground-truth"},
                   {"provider": "fake", "model_id": "obedient"}],
        "repeats": 1, "seed": 5, "max_tokens": 2048,
        "attack": {"injection_tasks": ["injection_task_4", "injection_task_7"]},
    }))
    store = tmp_path / "runs.sqlite"
    args = ["--store", str(store), "--cache", str(tmp_path / "cache.sqlite"),
            "--env-file", str(tmp_path / "no.env")]
    assert main(["run", str(config), *args]) == 0
    assert "6 runs, 0 already done, 6 to go" in capsys.readouterr().out

    db = sqlite3.connect(store)
    rows = set(db.execute(
        "SELECT model_id, injection_task_id, outcome, utility, attack_succeeded,"
        " utility_under_attack FROM runs"
    ))
    assert rows == {
        ("ground-truth", None, "solved", 1, None, None),
        ("ground-truth", "injection_task_4", "solved", 1, 0, 1),
        ("ground-truth", "injection_task_7", "solved", 1, 0, 1),
        ("obedient", None, "solved", 1, None, None),
        ("obedient", "injection_task_4", "solved", 1, 1, 0),
        ("obedient", "injection_task_7", "solved", 1, 1, 0),
    }
    attacks = db.execute("SELECT DISTINCT attack FROM runs WHERE attack IS NOT NULL")
    assert list(attacks) == [(ATTACK_NAME,)]
    db.close()

    # A second invocation finds everything done and sends nothing.
    capsys.readouterr()
    assert main(["run", str(config), *args]) == 0
    assert "6 runs, 6 already done, 0 to go" in capsys.readouterr().out

    assert main(["report", "attack_smoke", "--store", str(store), "--config", str(config)]) == 0
    report = capsys.readouterr().out
    assert "Runs stored: 6 of 6" in report
    assert "4 attack run(s) are stored but not shown" in report
    # Benign utility counts the two benign runs only, one per model.
    assert report.count("all tasks             1/1 = 100.0%") == 2
