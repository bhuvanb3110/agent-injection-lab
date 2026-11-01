"""The six M4 configs (PLAN_M3 step 11, PREREGISTRATION.md section 4): configs only, nothing is run.

They must be exactly what the preregistration says: the subset files' cases, the repeats, the run
counts of PLAN_M3 §0 and D99, and no case outside the test-screen sample.
"""

import json
from pathlib import Path

import pytest

from lab.config import ExperimentConfig, expand, load_experiment
from lab.models.providers import load_providers

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "attack_cases"
CONFIGS = ROOT / "configs" / "experiments"
ALL_SIX = ["t1", "t2", "t3", "t4", "t5", "t5_summary"]
M20, M120 = "openai/gpt-oss-20b", "openai/gpt-oss-120b"
SEED = 20261101


def ids_of(subset: str) -> list[str]:
    return json.loads((DATA / f"banking_{subset}.json").read_text(encoding="utf-8"))["ids"]


def load(name: str) -> ExperimentConfig:
    return load_experiment(CONFIGS / f"{name}.yaml")


REPEAT_IN_120B = [i for i in ids_of("repeat") if i in set(ids_of("120b"))]

# name -> (model, the case list it must name, the repeat indices, the number of runs)
CASE_CONFIGS = {
    "m4_topology_20b": (M20, ids_of("test_screen"), [0], 300),
    "m4_topology_20b_repeats": (M20, ids_of("repeat"), [1, 2], 120),
    "m4_topology_120b": (M120, ids_of("120b"), [0], 150),
    "m4_topology_120b_repeats": (M120, REPEAT_IN_120B, [1, 2], 60),
}


@pytest.mark.parametrize("name", CASE_CONFIGS)
def test_a_topology_config_names_exactly_its_subset(name: str) -> None:
    model, ids, repeats, n_runs = CASE_CONFIGS[name]
    config = load(name)
    assert config.name == name
    assert config.cases == ids  # the subset file's order
    assert config.repeat_indices() == repeats
    assert config.topology_names() == ALL_SIX
    assert [m.model_id for m in config.models] == [model]
    assert config.seed == SEED and config.max_tokens == 2048
    assert config.include_benign is False and config.attack is None
    specs = expand(config, load_providers())
    assert len(specs) == n_runs
    assert {s.model_id for s in specs} == {model}
    assert {s.seed for s in specs} == {SEED + r for r in repeats}


def test_the_case_lists_are_test_screen_cases_and_nested() -> None:
    split = json.loads((DATA / "banking_split.json").read_text(encoding="utf-8"))["cases"]
    screen = set(ids_of("test_screen"))
    for name in CASE_CONFIGS:
        ids = load(name).cases
        assert isinstance(ids, list) and len(set(ids)) == len(ids), name
        assert set(ids) <= screen, name
        assert {split[i] for i in ids} == {"test"}, name
    assert len(REPEAT_IN_120B) == 5  # D92: 5 of the 10 repeat cases are in the 25


def test_the_run_counts_are_the_plans() -> None:
    """PLAN_M3 §0: 20b 70 case-runs (50 + 10 x 2), 120b 35 (25 + 5 x 2), six topologies each."""
    providers = load_providers()
    runs = {m: 0 for m in (M20, M120)}
    for name, (model, _, _, _) in CASE_CONFIGS.items():
        runs[model] += len(expand(load(name), providers))
    assert runs == {M20: 70 * 6, M120: 35 * 6}


@pytest.mark.parametrize("name,model", [("m4_benign_20b", M20), ("m4_benign_120b", M120)])
def test_a_benign_config_is_sixteen_tasks_without_an_attack(name: str, model: str) -> None:
    config = load(name)
    assert config.attack is None and config.cases is None and config.include_benign
    assert config.user_tasks == "all" and config.task_ids().__len__() == 16
    assert config.repeat_indices() == [0] and config.topology_names() == ALL_SIX
    assert config.seed == SEED
    specs = expand(config, load_providers())
    assert len(specs) == 16 * 6
    assert {s.model_id for s in specs} == {model}
    assert all(s.injection_task_id is None and s.case_id is None for s in specs)


def test_no_m4_config_names_a_defense() -> None:
    """The defense sweep's configs are registered through the freeze log in M4, not here."""
    for path in CONFIGS.glob("m4_*.yaml"):
        assert "configs/defenses" not in path.read_text(encoding="utf-8"), path.name
