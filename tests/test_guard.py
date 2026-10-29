"""The `lab run` guard and `prereg_sha256` (PLAN_M3 step 10, D102, D117).

`lab run` refuses an experiment with test-split cases unless group A passes, and an absent
PREREGISTRATION.md fails group A. Dev-case experiments pass through untouched. FakeModels only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from lab import cli
from lab.cli import main
from lab.config import ExperimentConfig, expand
from lab.errors import ConfigError
from lab.models.providers import load_providers
from lab.rules import PROJECT_ROOT
from lab.rules.checks import Check, run_group_a
from lab.rules.guard import check_run
from lab.rules.prereg import prereg_sha256
from lab.rules.scope import baseline_case_id, load_split_data
from lab.scheduler.spec import RunSpec
from lab.store import RunStore

WritePrereg = Callable[..., Path]
SPLIT = load_split_data(PROJECT_ROOT)
DEV_CASE = "banking:plain_instruction:unauthorized_action:r0"
TEST_CASE = next(i for i in SPLIT.test_screen if "important_instructions" in i)
assert SPLIT.split_of[DEV_CASE] == "dev" and SPLIT.split_of[TEST_CASE] == "test"
DEV_PAIR = ("user_task_2", "injection_task_4")  # D84, dev
TEST_PAIR = ("user_task_3", "injection_task_7")
assert SPLIT.split_of[baseline_case_id(*DEV_PAIR)] == "dev"
assert SPLIT.split_of[baseline_case_id(*TEST_PAIR)] == "test"


def experiment(name: str = "guarded", **overrides: Any) -> dict[str, Any]:
    return {
        "name": name, "suite": "banking", "topology": "t1", "user_tasks": ["user_task_0"],
        "models": [{"provider": "fake", "model_id": "ground-truth"}], "repeats": 1, "seed": 7,
        "max_tokens": 2048, "include_benign": False, "cases": [DEV_CASE],
    } | overrides


def write(path: Path, body: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(body), encoding="utf-8", newline="\n")
    return path


def config_of(body: dict[str, Any]) -> ExperimentConfig:
    return ExperimentConfig.model_validate(body)


@pytest.fixture
def cached_group_a(repo: Path, regenerated: dict[str, str],
                   monkeypatch: pytest.MonkeyPatch) -> Callable[[Path], list[Check]]:
    """Group A with the generator's output supplied, so the guard's checks run for real and fast.
    The guard looks `run_group_a` up in its own module, which is patched here."""
    def group_a(root: Path) -> list[Check]:
        return run_group_a(root, regenerated)

    monkeypatch.setattr("lab.rules.guard.run_group_a", group_a)
    return group_a


# --- what the guard decides ---------------------------------------------------------------------


def test_a_dev_case_experiment_passes_untouched_even_without_a_prereg(tmp_path: Path) -> None:
    body = experiment()
    assert check_run(config_of(body), write(tmp_path / "dev.yaml", body), PROJECT_ROOT) is None


@pytest.mark.parametrize("cases", [
    [TEST_CASE],
    [DEV_CASE, TEST_CASE],  # one test case among dev cases refuses the whole experiment
    "test_screen",
    "banking_v1",  # every case, test ones included
    "repeat",
])
def test_an_experiment_with_a_test_case_is_refused_without_a_prereg(
    tmp_path: Path, cases: object
) -> None:
    body = experiment(cases=cases)
    with pytest.raises(ConfigError, match=r"contains test-split cases.*does not exist"):
        check_run(config_of(body), write(tmp_path / "t.yaml", body), PROJECT_ROOT)


def test_an_attack_config_is_judged_by_the_split_of_its_pairs(tmp_path: Path) -> None:
    """Milestone 2's `attack:` configs have no case list: each (user task, injection task) is the
    AgentDojo case of its pair."""
    def attack(pair: tuple[str, str]) -> dict[str, Any]:
        return experiment(cases=None, include_benign=True, user_tasks=[pair[0]],
                          attack={"injection_tasks": [pair[1]]})

    dev, test = attack(DEV_PAIR), attack(TEST_PAIR)
    assert check_run(config_of(dev), write(tmp_path / "d.yaml", dev), PROJECT_ROOT) is None
    with pytest.raises(ConfigError, match="contains test-split cases"):
        check_run(config_of(test), write(tmp_path / "t.yaml", test), PROJECT_ROOT)


def test_every_checked_in_experiment_config_is_dev_only_or_benign() -> None:
    """So the guard changes nothing for the configs that exist today (CLAUDE.md's commands)."""
    for path in sorted((PROJECT_ROOT / "configs" / "experiments").glob("*.yaml")):
        config = ExperimentConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert check_run(config, path, PROJECT_ROOT) is None, path.name


def test_an_unknown_case_or_set_is_refused_not_guessed(tmp_path: Path) -> None:
    for cases in (["banking:no:such:case"], "no_such_set"):
        body = experiment(cases=cases)
        with pytest.raises(ConfigError, match="cannot tell which split"):
            check_run(config_of(body), write(tmp_path / "u.yaml", body), PROJECT_ROOT)


def test_a_test_case_experiment_passes_when_group_a_does_and_gets_the_prereg_hash(
    repo: Path, tmp_path: Path, cached_group_a: Callable[[Path], list[Check]]
) -> None:
    body = experiment(cases=[TEST_CASE])
    got = check_run(config_of(body), write(tmp_path / "t.yaml", body), repo)
    assert got == prereg_sha256(repo) and got is not None and len(got) == 64


def test_a_test_case_experiment_is_refused_when_group_a_fails(
    repo: Path, tmp_path: Path, cached_group_a: Callable[[Path], list[Check]]
) -> None:
    path = repo / "data/attack_cases/banking_test_screen.json"
    path.write_bytes(path.read_bytes() + b"\n")  # tampered after pinning
    body = experiment(cases=[TEST_CASE])
    with pytest.raises(ConfigError, match=r"group A fails.*A1 pinned hashes: .*raw bytes differ"):
        check_run(config_of(body), write(tmp_path / "t.yaml", body), repo)


def test_a_dev_experiment_is_not_checked_even_when_group_a_would_fail(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "PREREGISTRATION.md").unlink()
    body = experiment()
    assert check_run(config_of(body), write(tmp_path / "d.yaml", body), repo) is None


def test_group_a_is_only_run_for_a_registered_experiment(tmp_path: Path) -> None:
    calls: list[Path] = []

    def spy(root: Path) -> list[Check]:
        calls.append(root)
        return []

    body = experiment()
    check_run(config_of(body), write(tmp_path / "d.yaml", body), PROJECT_ROOT, group_a=spy)
    assert calls == []


# --- benign controls pinned in the prereg -------------------------------------------------------


def benign(name: str = "controls") -> dict[str, Any]:
    return experiment(name, cases=None, include_benign=True, user_tasks=["user_task_0"])


def test_a_pinned_benign_config_is_a_registered_experiment(
    repo: Path, tmp_path: Path, write_prereg: WritePrereg,
    cached_group_a: Callable[[Path], list[Check]],
) -> None:
    """The benign controls have no cases, so nothing else says they belong to M4: the pin does."""
    body = benign()
    config = repo / "configs/experiments/controls.yaml"
    write(config, body)
    # Not pinned yet: a plain experiment, passes through with no hash.
    assert check_run(config_of(body), config, repo) is None
    write_prereg(repo, extra_pins={"configs/experiments/controls.yaml": cli_hash(config)})
    assert check_run(config_of(body), config, repo) == prereg_sha256(repo)
    (repo / "configs/golden/behaviour_v1.yaml").write_bytes(b"version: 1\n")  # group A now fails
    with pytest.raises(ConfigError, match="is pinned in the prereg"):
        check_run(config_of(body), config, repo)


def cli_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_pinned_dev_case_list_is_not_registered(
    repo: Path, write_prereg: WritePrereg
) -> None:
    """Pinning a step-8-style config must not change its run IDs, or a resumed run would start
    over."""
    body = experiment()
    config = repo / "configs/experiments/devlist.yaml"
    write(config, body)
    write_prereg(repo, extra_pins={"configs/experiments/devlist.yaml": cli_hash(config)})
    assert check_run(config_of(body), config, repo) is None


# --- prereg_sha256 on the run spec --------------------------------------------------------------


def spec(**fields: Any) -> RunSpec:
    base: dict[str, Any] = {
        "experiment": "e", "suite": "banking", "benchmark_version": "v1.2.2",
        "user_task_id": "user_task_0", "topology": "t1", "provider": "fake",
        "model_id": "ground-truth", "temperature": 0.6, "reasoning_effort": None,
        "max_tokens": 2048, "seed": 1, "repeat_idx": 0,
    }
    return RunSpec(**(base | fields))


def test_the_default_leaves_the_run_id_alone_and_a_hash_changes_it() -> None:
    plain = spec()
    assert plain.prereg_sha256 is None
    stamped = spec(prereg_sha256="a" * 64)
    assert stamped.run_id != plain.run_id
    assert spec(prereg_sha256="b" * 64).run_id != stamped.run_id
    assert spec(prereg_sha256="a" * 64).run_id == stamped.run_id


def test_a_malformed_prereg_hash_is_refused() -> None:
    for bad in ("abc", "A" * 64, "g" * 64):
        with pytest.raises(ValueError, match="64 lowercase hex"):
            spec(prereg_sha256=bad)


def test_expand_stamps_every_spec_or_none_and_the_default_ids_are_the_old_ones() -> None:
    config = config_of(experiment(cases=[DEV_CASE, "banking:fake_authority:sabotage:r1"]))
    plain = expand(config, load_providers())
    stamped = expand(config, load_providers(), prereg_sha256="c" * 64)
    assert all(s.prereg_sha256 is None for s in plain)
    assert all(s.prereg_sha256 == "c" * 64 for s in stamped)
    assert all(a.run_id != b.run_id for a, b in zip(plain, stamped, strict=True))
    unstamped = [s.model_copy(update={"prereg_sha256": None}) for s in stamped]
    assert [s.run_id for s in unstamped] == [s.run_id for s in plain]


# --- `lab run` end to end -----------------------------------------------------------------------


def run_args(tmp_path: Path) -> list[str]:
    return ["--store", str(tmp_path / "runs.sqlite"), "--cache", str(tmp_path / "cache.sqlite"),
            "--env-file", str(tmp_path / "no.env")]


def test_lab_run_refuses_test_cases_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write(tmp_path / "t.yaml", experiment(cases=[TEST_CASE]))
    assert main(["run", str(config), *run_args(tmp_path)]) == 2
    assert "Test cases stay refused until it is written and committed" in capsys.readouterr().err
    assert not (tmp_path / "runs.sqlite").exists() and not (tmp_path / "cache.sqlite").exists()


def test_lab_run_runs_dev_cases_as_before_with_no_prereg_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write(tmp_path / "d.yaml", experiment(cases=[DEV_CASE]))
    assert main(["run", str(config), *run_args(tmp_path)]) == 0
    assert "Registered experiment" not in capsys.readouterr().out
    store = RunStore(tmp_path / "runs.sqlite")
    try:
        (run_id,) = store.run_ids("guarded")
        assert store.get_run(run_id).spec.prereg_sha256 is None
        assert run_id == expand(config_of(experiment(cases=[DEV_CASE])), load_providers())[0].run_id
    finally:
        store.close()
    with sqlite3.connect(tmp_path / "runs.sqlite") as db:
        assert db.execute("SELECT prereg_sha256 FROM runs").fetchall() == [(None,)]


def test_lab_run_stamps_a_test_case_run_once_group_a_passes(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    cached_group_a: Callable[[Path], list[Check]], capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "PROJECT_ROOT", repo)
    body = experiment("m4_like", cases=[TEST_CASE])
    config = write(tmp_path / "t.yaml", body)
    assert main(["run", str(config), *run_args(tmp_path)]) == 0
    assert "Registered experiment" in capsys.readouterr().out
    hash_ = prereg_sha256(repo)
    store = RunStore(tmp_path / "runs.sqlite")
    try:
        (run_id,) = store.run_ids("m4_like")
        record = store.get_run(run_id)
        assert record.spec.prereg_sha256 == hash_ and record.spec.case_id == TEST_CASE
        assert run_id == expand(config_of(body), load_providers(), prereg_sha256=hash_)[0].run_id
        assert run_id != expand(config_of(body), load_providers())[0].run_id
    finally:
        store.close()
    with sqlite3.connect(tmp_path / "runs.sqlite") as db:
        assert db.execute("SELECT prereg_sha256 FROM runs").fetchall() == [(hash_,)]
    # Resumable: the same command again finds the run by its stamped ID and runs nothing.
    assert main(["run", str(config), *run_args(tmp_path)]) == 0
    assert "0 to go" in capsys.readouterr().out
