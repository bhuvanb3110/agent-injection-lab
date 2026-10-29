"""The step-8 dev configs (PLAN_M3 §8, D105): configs only, nothing is run here.

Each one names case IDs. They must be the grid's sets J and C, dev cases only, and add up to the
plan's run counts (20b 54 + 10, 120b 28).
"""

import json
from itertools import product
from pathlib import Path
from typing import Any

import pytest
import yaml

from lab.attacks.composition import load_composition
from lab.attacks.dataset import BANKING, DATA_DIR, Dataset, build_dataset
from lab.config import ExperimentConfig, expand, load_experiment
from lab.models.providers import load_providers

CONFIGS = Path(__file__).resolve().parents[1] / "configs" / "experiments"
NAMES = ("step8_20b_j", "step8_20b_c", "step8_120b")
ALL_SIX = ["t1", "t2", "t3", "t4", "t5", "t5_summary"]


@pytest.fixture(scope="module")
def data() -> Dataset:
    return build_dataset(BANKING, load_composition(DATA_DIR / "banking_composition.yaml"))


def grid_sets(data: Dataset) -> tuple[set[str], set[str]]:
    """J and C as PLAN_M3 §8 defines them, taken from the generated dataset."""
    dev = [c for c in data.cases[data.n_baseline :] if data.split_of[c.id] == "dev"]
    j = {c.id for c in dev if c.goal == "sabotage"}
    c_set = {c.id for c in dev if c.channel == "tool_description"} | {
        c.id for c in dev if c.technique == "multi_hop" and c.goal == "unauthorized_action"
    }
    return j, c_set


def load(name: str) -> ExperimentConfig:
    return load_experiment(CONFIGS / f"{name}.yaml")


def case_ids(name: str) -> list[str]:
    cases = load(name).cases
    assert isinstance(cases, list)
    return cases


def test_the_configs_contain_only_dev_cases() -> None:
    """Read from the committed split file, not the generator, so a regenerated one cannot."""
    split = json.loads((DATA_DIR / "banking_split.json").read_text(encoding="utf-8"))["cases"]
    for name in NAMES:
        ids = case_ids(name)
        assert ids and len(set(ids)) == len(ids), name
        assert {split[i] for i in ids} == {"dev"}, name


def test_the_sets_are_the_grids_j_and_c(data: Dataset) -> None:
    j, c_set = grid_sets(data)
    assert (len(j), len(c_set), len(j & c_set), len(j | c_set)) == (7, 9, 2, 14)
    assert set(case_ids("step8_20b_c")) == c_set
    assert set(case_ids("step8_20b_j")) == j - c_set
    assert set(case_ids("step8_120b")) == j | c_set


def test_topologies_models_and_repeats_are_the_plans() -> None:
    expected = {
        "step8_20b_j": (["t1", "t2"], "openai/gpt-oss-20b"),
        "step8_20b_c": (ALL_SIX, "openai/gpt-oss-20b"),
        "step8_120b": (["t1", "t2"], "openai/gpt-oss-120b"),
    }
    for name, (topologies, model) in expected.items():
        config = load(name)
        assert config.name == name and config.suite == "banking"
        assert config.topology_names() == topologies
        assert [(m.provider, m.model_id) for m in config.models] == [("groq", model)]
        assert config.repeat_indices() == [0]
        assert config.include_benign is False and config.attack is None


def test_the_run_counts_are_the_plans() -> None:
    runs = {name: len(case_ids(name)) * len(load(name).topology_names()) for name in NAMES}
    assert runs == {"step8_20b_j": 10, "step8_20b_c": 54, "step8_120b": 28}
    assert runs["step8_20b_j"] + runs["step8_20b_c"] == 64


def test_the_two_20b_configs_do_not_repeat_a_run() -> None:
    cells = [
        set(product(case_ids(n), load(n).topology_names())) for n in ("step8_20b_j", "step8_20b_c")
    ]
    assert cells[0].isdisjoint(cells[1])


def test_an_empty_case_list_is_refused() -> None:
    raw: dict[str, Any] = yaml.safe_load((CONFIGS / "step8_120b.yaml").read_text(encoding="utf-8"))
    raw["cases"] = []
    with pytest.raises(ValueError, match="at least one case"):
        ExperimentConfig.model_validate(raw)


def test_running_a_case_list_is_not_built_yet() -> None:
    """The step-8 run needs the runner to place the new payloads; until then expand() says so."""
    with pytest.raises(ValueError, match="not built"):
        expand(load("step8_120b"), load_providers())
