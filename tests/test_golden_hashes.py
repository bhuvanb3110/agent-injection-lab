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


# The same for the milestone-2 configs, which have attack runs. Taken from the code at commit
# 0379b8b (before the runner ran attack cases, M3 step 8), in a clean worktree. All 72
# m2_exit_v3 run IDs and v2's are also in the real run store. Same shape as GOLDEN.
GOLDEN_ATTACK = {
    "smoke_topologies": (
        "140e98505d802d2c67ca3a3ef04285f97a8b57754112b8a3240f587d15bb3298",
        48,
        "25ac2ef8ce6446f6475bdda2909ba6e3a1c6ea8dbf7de3a2a6e46c07bbedd753",
        "1e0604b908807422d7f40fd5aae22996431a205e2a06af709d10c14f39d4806a",
        "587aae3ecf8fc87ad353b43f9edc75c48ca23b92647b9759bffe6bdaf53ed2a8",
    ),
    "m2_exit_v3": (
        "e9c5c8606c65b819c51a43c73939376eaf4a9996f478c35b57698ab4878d2f5a",
        72,
        "352307daea6e5c6d2d1cc1c98d48421c0b77a05881c5417de73444f2d53b4f71",
        "0490a976d57f962b680944852b962cf115b554c502d8ca368718485a01c99edf",
        "edf5b7a78cf4974a54a15520c0b0dc2b799f06d07bec37402502a877438d919c",
    ),
    "m2_exit_v2": (
        "3ad6ca1d03d565d2437d6eb60dab0b49860f4c05d2b9c29ec0a1ccb5888f093d",
        72,
        "a96143ca6613b20fbd7cf3f890ae785c1f3678065a7a3022ab15e42b8c1b09fa",
        "c3309a9d6e922a49952d94e619f181418ff20e6ef2080261119fd18d87a08b45",
        "3ab6d2bd6862b9e7d5e20f964563349e2bf8728c3e90fa94e744ce6b97af06fa",
    ),
}


@pytest.mark.parametrize("name", GOLDEN_ATTACK)
def test_milestone_2_attack_configs_keep_their_hash_and_run_ids(name: str) -> None:
    config_hash, n_runs, digest, first, last = GOLDEN_ATTACK[name]
    config = load_experiment(EXPERIMENTS / f"{name}.yaml")
    specs = expand(config, load_providers())
    ids = [s.run_id for s in specs]
    assert config.config_hash == config_hash
    assert len(ids) == n_runs
    assert hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest() == digest
    assert (ids[0], ids[-1]) == (first, last)
    assert any(s.attack is not None for s in specs)
    assert all(s.case_id is None for s in specs)  # no case: the field is left out of the ID


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


def test_a_case_id_changes_the_run_id_and_none_leaves_it_alone() -> None:
    attack = {"attack": "plain_instruction", "injection_task_id": "injection_task_4"}
    without = spec(**attack)
    one = spec(**attack, case_id="banking:plain_instruction:unauthorized_action:r0")
    other = spec(**attack, case_id="banking:plain_instruction:unauthorized_action:r1")
    assert len({without.run_id, one.run_id, other.run_id}) == 3
    # The same spec with the field spelled out as None is the same run.
    assert spec(**attack, case_id=None).run_id == without.run_id


def test_a_case_id_needs_an_attack() -> None:
    with pytest.raises(ValueError, match="needs the case's attack"):
        spec(case_id="banking:plain_instruction:unauthorized_action:r0")
