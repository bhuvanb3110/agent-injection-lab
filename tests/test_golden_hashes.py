"""Milestone 2 added fields to RunSpec and ExperimentConfig. Milestone 1's stored runs must keep
their IDs (so a rerun skips them) and its configs their hashes (D48).

The values below were taken from the milestone-1 code (commit 293aa4b) and cross-checked against
the real store: every baseline_banking run ID and 58 of the 64 baseline_banking_repeats run IDs
were present in results/runs.sqlite, with these config hashes. They must never be regenerated
from the current code: that would make the test agree with whatever the code does.
"""

import hashlib
from pathlib import Path

import pytest

from lab.config import ExperimentConfig, expand, load_experiment
from lab.models.providers import load_providers
from lab.scheduler.spec import RunSpec

EXPERIMENTS = Path(__file__).resolve().parents[1] / "configs" / "experiments"

# experiment -> (config hash, number of runs, sha256 of the sorted run IDs joined by newlines,
#                first run ID in expansion order, last run ID in expansion order)
GOLDEN = {
    "smoke": (
        "7afec9557e16d332c50bbb7ad9d10c45fbd73af548a6befb4379335cf1ddda1b",
        8,
        "e8c3faaf6798c70c5afad43c058f9de6b882aa0c4a5fff5bf6a60f5bbdbc6dac",
        "63820b7ef912ca211340923a26177a17d723e7917bb46f3199af65bb6492e111",
        "a90de14856af9e8fdb2eed2c501daa0542cf623e778824ce9a4b12510dd25049",
    ),
    "baseline_banking": (
        "48a22e299dc90aad4f5195c3ae2f316c6cbd8a957d64be79d190f96a2b11e521",
        32,
        "b91aa230ff38f30a684409db1ad591e94d9461be071213eaa437b39babb47a6a",
        "92c263041b8b8d677028fec1b29a6ff99ff8b2e665be322645395e4ca59092e6",
        "0a1872cfa160df589c18d5372cf08ce9975b1801252e5a6f3f4e124b68bc3426",
    ),
    "baseline_banking_repeats": (
        "4a10bed3bd6f2bad271d3560a00b47a869ad594bd1966b1e47f8bb72e024d249",
        64,
        "2a6fee5f1d7f5ee6501428acfd69a5700eb6ba1d9eb5670ab10c587016886276",
        "192dfe32a4a83650f8a1d98c593c0b1479fd6de4197d66110dbf34cfc07aef33",
        "07dcf3822e2859bf9e57185fcfd258491b793f1c7faac2f5071d9e1bb6179f0e",
    ),
}


@pytest.mark.parametrize("name", GOLDEN)
def test_milestone_1_configs_keep_their_hash_and_run_ids(name: str) -> None:
    config_hash, n_runs, digest, first, last = GOLDEN[name]
    config = load_experiment(EXPERIMENTS / f"{name}.yaml")
    specs = expand(config, load_providers())
    ids = [s.run_id for s in specs]
    assert config.config_hash == config_hash
    assert len(ids) == n_runs
    assert hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest() == digest
    # The order matters too: it decides what a daily cap leaves undone.
    assert (ids[0], ids[-1]) == (first, last)
    assert all(s.attack is None and s.injection_task_id is None for s in specs)


def test_new_config_fields_at_their_defaults_do_not_change_the_hash() -> None:
    config = load_experiment(EXPERIMENTS / "smoke.yaml")
    explicit = ExperimentConfig.model_validate(
        config.model_dump(mode="json") | {"attack": None, "include_benign": True}
    )
    assert explicit.config_hash == config.config_hash
    # `topology: t1` and `topologies: [t1]` mean the same runs but are different configs.
    as_list = ExperimentConfig.model_validate(
        config.model_dump(mode="json") | {"topology": None, "topologies": ["t1"]}
    )
    assert as_list.config_hash != config.config_hash
    assert [s.run_id for s in expand(as_list, load_providers())] == [
        s.run_id for s in expand(config, load_providers())
    ]


def spec(**overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "experiment": "e", "suite": "banking", "benchmark_version": "v1.2.2",
        "user_task_id": "user_task_2", "topology": "t1", "provider": "fake",
        "model_id": "ground-truth", "temperature": 0.0, "reasoning_effort": None,
        "max_tokens": 2048, "seed": 1, "repeat_idx": 0,
    }
    return RunSpec(**(fields | overrides))  # type: ignore[arg-type]


def test_an_attack_changes_the_run_id() -> None:
    benign = spec()
    attacked = spec(attack="important_instructions_no_model_name",
                    injection_task_id="injection_task_4")
    other_injection = spec(attack="important_instructions_no_model_name",
                           injection_task_id="injection_task_5")
    assert len({benign.run_id, attacked.run_id, other_injection.run_id}) == 3


def test_attack_and_injection_task_go_together() -> None:
    with pytest.raises(ValueError, match="both be set"):
        spec(attack="important_instructions_no_model_name")
    with pytest.raises(ValueError, match="both be set"):
        spec(injection_task_id="injection_task_4")
