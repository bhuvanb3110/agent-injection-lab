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
CONFIRM_CASE = next(i for i, s in SPLIT.split_of.items()
                    if s == "test" and i not in SPLIT.test_screen
                    and "important_instructions" in i)
DEV_PAIR = ("user_task_2", "injection_task_4")  # D84, dev
TEST_PAIR = ("user_task_3", "injection_task_7")  # test-confirm
assert SPLIT.split_of[baseline_case_id(*DEV_PAIR)] == "dev"
assert SPLIT.is_test_confirm(baseline_case_id(*TEST_PAIR))


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
    "repeat",
])
def test_an_experiment_with_a_test_case_is_refused_without_a_prereg(
    repo: Path, tmp_path: Path, cases: object
) -> None:
    # A root without PREREGISTRATION.md: the real root has one since step 11.
    (repo / "PREREGISTRATION.md").unlink()
    body = experiment(cases=cases)
    with pytest.raises(ConfigError, match=r"contains test-split cases.*does not exist"):
        check_run(config_of(body), write(tmp_path / "t.yaml", body), repo)


def test_an_attack_config_is_judged_by_the_split_of_its_pairs(repo: Path, tmp_path: Path) -> None:
    """Milestone 2's `attack:` configs have no case list: each (user task, injection task) is the
    AgentDojo case of its pair."""
    def attack(pair: tuple[str, str]) -> dict[str, Any]:
        return experiment(cases=None, include_benign=True, user_tasks=[pair[0]],
                          attack={"injection_tasks": [pair[1]]})

    screen_pair = tuple(TEST_CASE.split(":")[2:4])
    dev, screen, confirm = attack(DEV_PAIR), attack(screen_pair), attack(TEST_PAIR)  # type: ignore[arg-type]
    (repo / "PREREGISTRATION.md").unlink()  # the real root has one since step 11
    assert check_run(config_of(dev), write(tmp_path / "d.yaml", dev), repo) is None
    with pytest.raises(ConfigError, match="contains test-split cases"):
        check_run(config_of(screen), write(tmp_path / "s.yaml", screen), repo)
    with pytest.raises(ConfigError, match="contains test-confirm cases"):
        check_run(config_of(confirm), write(tmp_path / "c.yaml", confirm), repo)


@pytest.mark.parametrize("cases", [[CONFIRM_CASE], [TEST_CASE, CONFIRM_CASE], "banking_v1"])
def test_test_confirm_cases_are_refused_until_the_confirmation_record(
    repo: Path, tmp_path: Path, freeze_log: Any, cached_group_a: Callable[[Path], list[Check]],
    cases: object,
) -> None:
    """Group A passes and the freeze log has the six freezes and the test-screen unblinding, which
    is all test-screen runs need; test-confirm cases still need the M6 registration."""
    freeze_log.write([*freeze_log.freezes(), freeze_log.unblinding()])
    body = experiment(cases=cases)
    config = write(tmp_path / "c.yaml", body)
    with pytest.raises(ConfigError, match=r"contains test-confirm cases.*no confirmation"):
        check_run(config_of(body), config, repo)
    freeze_log.write(freeze_log.full())
    assert check_run(config_of(body), config, repo) == prereg_sha256(repo)


def test_test_screen_cases_do_not_need_the_confirmation_record(
    repo: Path, tmp_path: Path, cached_group_a: Callable[[Path], list[Check]]
) -> None:
    body = experiment(cases="test_screen")  # no freeze log at all
    assert check_run(config_of(body), write(tmp_path / "s.yaml", body), repo) == (
        prereg_sha256(repo))


def test_a_broken_confirmation_keeps_test_confirm_refused(
    repo: Path, tmp_path: Path, freeze_log: Any
) -> None:
    freeze_log.write(freeze_log.full())
    (repo / "configs/m6_selection.yaml").write_text("confirm: [t3]\n", encoding="utf-8")
    body = experiment(cases=[CONFIRM_CASE])
    with pytest.raises(ConfigError, match="changed after it was registered"):
        check_run(config_of(body), write(tmp_path / "c.yaml", body), repo)


def test_a_broken_chain_keeps_test_confirm_refused(
    repo: Path, tmp_path: Path, freeze_log: Any
) -> None:
    path = freeze_log.write(freeze_log.full())
    path.write_text(path.read_text(encoding="utf-8").replace("2026-11-20T09:00:00Z",
                                                            "2026-11-19T09:00:00Z", 1),
                    encoding="utf-8")
    body = experiment(cases=[CONFIRM_CASE])
    with pytest.raises(ConfigError, match="chain is broken"):
        check_run(config_of(body), write(tmp_path / "c.yaml", body), repo)


def test_every_checked_in_experiment_config_is_dev_only_or_a_registered_m4_one() -> None:
    """The guard changes nothing for the configs that existed before step 11 (CLAUDE.md's
    commands). The six M4 configs are pinned in the prereg, so they are registered: they get the
    prereg's hash (group A is stubbed here; the real check is `lab validate-rules`)."""

    stamped = {}
    for path in sorted((PROJECT_ROOT / "configs" / "experiments").glob("*.yaml")):
        config = ExperimentConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        stamped[path.name] = check_run(config, path, PROJECT_ROOT, group_a=lambda root: [])
    expected = prereg_sha256(PROJECT_ROOT)
    assert expected is not None
    m4 = {name for name in stamped if name.startswith("m4_")}
    assert len(m4) == 6
    assert {n: h for n, h in stamped.items() if h is not None} == {n: expected for n in m4}


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


def test_the_prereg_hash_is_recorded_but_never_part_of_the_run_id() -> None:
    plain = spec()
    assert plain.prereg_sha256 is None
    stamped = spec(prereg_sha256="a" * 64)
    assert stamped.prereg_sha256 == "a" * 64
    assert stamped.run_id == plain.run_id == spec(prereg_sha256="b" * 64).run_id


def test_a_malformed_prereg_hash_is_refused() -> None:
    for bad in ("abc", "A" * 64, "g" * 64):
        with pytest.raises(ValueError, match="64 lowercase hex"):
            spec(prereg_sha256=bad)


def test_expand_stamps_every_spec_and_an_amendment_changes_no_run_id() -> None:
    config = config_of(experiment(cases=[DEV_CASE, "banking:fake_authority:sabotage:r1"]))
    plain = expand(config, load_providers())
    version_1 = expand(config, load_providers(), prereg_sha256="c" * 64)
    version_2 = expand(config, load_providers(), prereg_sha256="d" * 64)  # after an amendment
    assert all(s.prereg_sha256 is None for s in plain)
    assert all(s.prereg_sha256 == "c" * 64 for s in version_1)
    assert all(s.prereg_sha256 == "d" * 64 for s in version_2)
    assert [s.run_id for s in plain] == [s.run_id for s in version_1] == [
        s.run_id for s in version_2]


# --- `lab run` end to end -----------------------------------------------------------------------


def run_args(tmp_path: Path) -> list[str]:
    return ["--store", str(tmp_path / "runs.sqlite"), "--cache", str(tmp_path / "cache.sqlite"),
            "--env-file", str(tmp_path / "no.env")]


def test_lab_run_refuses_test_cases_and_creates_nothing(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "PREREGISTRATION.md").unlink()  # the real root has one since step 11
    monkeypatch.setattr(cli, "PROJECT_ROOT", repo)
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
        assert run_id == expand(config_of(body), load_providers())[0].run_id  # not in the ID
    finally:
        store.close()
    with sqlite3.connect(tmp_path / "runs.sqlite") as db:
        assert db.execute("SELECT prereg_sha256 FROM runs").fetchall() == [(hash_,)]
    # Resumable: the same command again finds the run by its ID and runs nothing.
    assert main(["run", str(config), *run_args(tmp_path)]) == 0
    assert "0 to go" in capsys.readouterr().out


def test_an_amendment_between_two_invocations_neither_reruns_nor_restamps(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    cached_group_a: Callable[[Path], list[Check]], write_prereg: WritePrereg,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The prereg is amended (appended to, so its hash changes) while a sweep is half done. The
    runs already stored keep the hash of the version they ran under and are not run again; the
    rest are stamped with the new version."""
    monkeypatch.setattr(cli, "PROJECT_ROOT", repo)
    first = TEST_CASE
    second = next(i for i in sorted(SPLIT.test_screen) if i != first)
    config = write(tmp_path / "t.yaml", experiment("sweep", cases=[first]))
    assert main(["run", str(config), *run_args(tmp_path)]) == 0
    version_1 = prereg_sha256(repo)
    write_prereg(repo, text_after="\n## Amendment 1 (dated)\n")
    version_2 = prereg_sha256(repo)
    assert version_1 != version_2
    write(config, experiment("sweep", cases=[first, second]))
    capsys.readouterr()
    assert main(["run", str(config), *run_args(tmp_path)]) == 0
    assert "1 already done, 1 to go" in capsys.readouterr().out
    store = RunStore(tmp_path / "runs.sqlite")
    try:
        stamped = {r.spec.case_id: r.spec.prereg_sha256 for r in store.runs("sweep")}
    finally:
        store.close()
    assert stamped == {first: version_1, second: version_2}


# --- no way around the guard from `lab run` ------------------------------------------------------


def test_there_is_no_flag_environment_variable_or_config_field_that_skips_the_guard(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    """`no_guard` is a pytest fixture in tests/conftest.py and nothing else: it patches the name
    `check_run` inside `lab.cli` for one test. Nothing under lab/ mentions it, `lab run` has no
    option for it, and neither the environment, the .env file nor the config can switch it."""
    lab_dir = PROJECT_ROOT / "lab"
    for path in lab_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "no_guard" not in text and "noguard" not in text and "skip_guard" not in text, path
    (repo / "PREREGISTRATION.md").unlink()  # the real root has one since step 11
    monkeypatch.setattr(cli, "PROJECT_ROOT", repo)
    for variable in ("LAB_NO_GUARD", "NO_GUARD", "LAB_SKIP_GUARD"):
        monkeypatch.setenv(variable, "1")
    (tmp_path / "x.env").write_text("LAB_NO_GUARD=1\nNO_GUARD=true\n", encoding="utf-8")
    config = write(tmp_path / "t.yaml", experiment(cases=[TEST_CASE]))
    args = [*run_args(tmp_path)[:-1], str(tmp_path / "x.env")]
    assert main(["run", str(config), *args]) == 2
    assert "contains test-split cases" in capsys.readouterr().err
    assert not (tmp_path / "runs.sqlite").exists()
    with pytest.raises(SystemExit):  # an option that does not exist
        main(["run", str(config), "--no-guard"])
    with pytest.raises(ValueError, match="no_guard"):  # a field that does not exist
        config_of(experiment(cases=[TEST_CASE]) | {"no_guard": True})
