"""`lab report --blind`, the blinding of test-screen outcomes, and `lab report` grouping runs by
case (PLAN_M3 step 10, D103, D117). FakeModels only, no network.

Runs of test-split cases are made with the guard switched off (`no_guard`), which is how a test
store gets them; what these tests are about is what `lab report` and `lab trace` then show.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from lab import cli
from lab.cli import blind_text, main
from lab.models.providers import load_providers
from lab.rules import PROJECT_ROOT
from lab.rules.scope import load_split_data
from lab.store import RunStore

SPLIT = load_split_data(PROJECT_ROOT)
SCREEN_CASE = next(i for i in sorted(SPLIT.test_screen) if "important_instructions" in i)
TEST_CONFIRM_CASE = next(i for i, s in SPLIT.split_of.items()
                         if s == "test" and i not in SPLIT.test_screen
                         and "important_instructions" in i)
DEV_CASE = "banking:plain_instruction:unauthorized_action:r0"
SAME_PAIR = ["banking:fake_completion:sabotage:r0", "banking:context_blending:sabotage:r2"]

# Words of the outcome sections that a blinded view must not contain.
OUTCOME_WORDS = ("ASR", "attacker effect", "utility", "solved", "outcomes", "benign utility",
                 "propagated", "infection depth", "acted on", "gate decisions", "flips")


def write_config(path: Path, name: str, cases: list[str], model: str = "ground-truth") -> Path:
    path.write_text(yaml.safe_dump({
        "name": name, "suite": "banking", "topologies": ["t1", "t2"], "user_tasks": ["user_task_0"],
        "models": [{"provider": "fake", "model_id": model}], "repeats": 1, "seed": 11,
        "max_tokens": 2048, "include_benign": False, "cases": cases,
    }), encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def store_path(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch,
               no_guard: None) -> Path:
    """A store with runs of a test-screen case, a test-confirm case and a dev case, each in its
    own experiment. The code's project root is the fixture repository, so the freeze log the
    tests write is the one `lab report` reads."""
    monkeypatch.setattr(cli, "PROJECT_ROOT", repo)
    for name, case in (("screen", SCREEN_CASE), ("confirm", TEST_CONFIRM_CASE), ("dev", DEV_CASE)):
        config = write_config(tmp_path / f"{name}.yaml", name, [case])
        assert main(["run", str(config), "--store", str(tmp_path / "runs.sqlite"),
                     "--cache", str(tmp_path / "cache.sqlite"),
                     "--env-file", str(tmp_path / "no.env")]) == 0
    return tmp_path / "runs.sqlite"


def report(store_path: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> str:
    capsys.readouterr()
    assert main(["report", *args, "--store", str(store_path)]) == 0
    return capsys.readouterr().out


def views(store_path: Path) -> list[tuple[str, str, str, int]]:
    with sqlite3.connect(store_path) as db:
        return db.execute("SELECT command, detail, scope, runs FROM outcome_views "
                          "ORDER BY id").fetchall()


# --- blinded ------------------------------------------------------------------------------------


def test_test_screen_outcomes_are_hidden_until_there_is_an_unblinding_record(
    store_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = report(store_path, "screen", capsys=capsys)
    assert "Blinded (D103): 2 run(s). Counts, errors, tokens and cost only." in out
    assert "FREEZE_LOG.yaml" in out  # why: there is no freeze log
    for word in OUTCOME_WORDS:
        assert word not in out, word
    # What it does show: counts, errors, tokens, cost.
    assert "runs stored            1" in out
    assert "model errors" in out and "tokens" in out and "est. cost" in out and "calls" in out
    assert views(store_path) == []  # nothing was shown, so nothing is logged


def test_a_dev_experiment_is_reported_in_full(
    store_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = report(store_path, "dev", capsys=capsys)
    assert "Blinded" not in out and "ASR" in out and "attacker effect" in out
    assert views(store_path) == []


def test_blind_hides_the_outcomes_of_a_dev_experiment_too(
    store_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = report(store_path, "dev", "--blind", capsys=capsys)
    assert "why: --blind" in out
    for word in OUTCOME_WORDS:
        assert word not in out, word
    assert "runs stored            1" in out


def test_a_mixed_report_hides_the_screen_runs_and_keeps_the_rest(
    store_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = report(store_path, "dev", "screen", capsys=capsys)
    assert "Blinded (D103): 2 run(s)" in out  # the screen experiment's two topologies
    assert "ASR" in out  # the dev experiment's
    assert "Runs stored: 4 (" in out  # the count covers every run, hidden ones too


def test_test_confirm_outcomes_are_blinded_like_test_screen_ones(
    store_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = report(store_path, "confirm", capsys=capsys)
    assert "Blinded (D103): 2 run(s)" in out and "test-confirm" in out
    for word in OUTCOME_WORDS:
        assert word not in out, word
    assert views(store_path) == []


def test_the_blind_text_has_no_rate_built_from_an_outcome(
    store_path: Path,
) -> None:
    store = RunStore(store_path)
    try:
        runs = store.runs("screen")
    finally:
        store.close()
    prices = {(p.name, m): i.price for p in load_providers().values() for m, i in p.models.items()}
    text = blind_text(runs, prices, "test")
    assert "%" not in text and "n/a" not in text
    for word in OUTCOME_WORDS:
        assert word not in text, word


# --- unblinding -----------------------------------------------------------------------------------


def test_a_valid_unblinding_record_shows_the_outcomes_and_logs_the_view(
    store_path: Path, freeze_log: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    freeze_log.write([*freeze_log.freezes(), freeze_log.unblinding()])
    out = report(store_path, "screen", capsys=capsys)
    assert "Blinded" not in out and "ASR" in out and "attacker effect" in out
    assert views(store_path) == [("report", "screen", "test_screen", 2)]


def test_the_screen_unblinding_does_not_unblind_test_confirm(
    store_path: Path, freeze_log: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    freeze_log.write([*freeze_log.freezes(), freeze_log.unblinding()])
    out = report(store_path, "confirm", capsys=capsys)
    assert "Blinded" in out and "ASR" not in out
    assert "no test_confirm unblinding record" in out


def test_a_confirm_unblinding_record_shows_test_confirm_outcomes_and_logs_the_view(
    store_path: Path, freeze_log: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    freeze_log.write([*freeze_log.full(),
                      freeze_log.unblinding(at="2026-12-20T00:00:00Z", scope="test_confirm")])
    out = report(store_path, "confirm", "screen", capsys=capsys)
    assert "Blinded" not in out and "ASR" in out
    assert views(store_path) == [("report", "confirm, screen", "test_screen", 2),
                                 ("report", "confirm, screen", "test_confirm", 2)]


def test_a_confirm_unblinding_without_a_valid_confirmation_is_refused(
    store_path: Path, freeze_log: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    freeze_log.write([*freeze_log.freezes(), freeze_log.unblinding(),
                      freeze_log.unblinding(at="2026-12-20T00:00:00Z", scope="test_confirm")])
    out = report(store_path, "confirm", capsys=capsys)
    assert "Blinded" in out and "ASR" not in out and "refused" in out
    assert views(store_path) == []


def test_unblinding_before_all_six_defenses_are_frozen_is_refused(
    store_path: Path, freeze_log: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    freeze_log.write([*freeze_log.freezes(but=("everything",)), freeze_log.unblinding()])
    out = report(store_path, "screen", capsys=capsys)
    assert "refused (defenses not frozen: everything)" in out
    for word in OUTCOME_WORDS:
        assert word not in out, word
    assert views(store_path) == []


def test_blind_still_hides_after_the_unblinding_record(
    store_path: Path, freeze_log: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    freeze_log.write([*freeze_log.freezes(), freeze_log.unblinding()])
    out = report(store_path, "screen", "--blind", capsys=capsys)
    assert "Blinded" in out and "ASR" not in out
    assert views(store_path) == []


# --- lab trace ------------------------------------------------------------------------------------


def run_id_of(store_path: Path, experiment: str) -> str:
    store = RunStore(store_path)
    try:
        return store.run_ids(experiment)[0]
    finally:
        store.close()


def test_trace_refuses_a_blinded_test_screen_run_and_shows_it_once_unblinded(
    store_path: Path, freeze_log: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = run_id_of(store_path, "screen")
    capsys.readouterr()
    assert main(["trace", run_id[:12], "--store", str(store_path)]) == 1
    captured = capsys.readouterr()
    assert "stays blinded" in captured.err and captured.out == ""
    freeze_log.write([*freeze_log.freezes(), freeze_log.unblinding()])
    assert main(["trace", run_id[:12], "--store", str(store_path)]) == 0
    assert capsys.readouterr().out
    assert views(store_path) == [("trace", run_id, "test_screen", 1)]


def test_trace_refuses_a_blinded_test_confirm_run_until_its_own_record(
    store_path: Path, freeze_log: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = run_id_of(store_path, "confirm")
    freeze_log.write([*freeze_log.freezes(), freeze_log.unblinding()])
    capsys.readouterr()
    assert main(["trace", run_id[:12], "--store", str(store_path)]) == 1
    assert "test-confirm run" in capsys.readouterr().err
    freeze_log.write([*freeze_log.full(),
                      freeze_log.unblinding(at="2026-12-20T00:00:00Z", scope="test_confirm")])
    assert main(["trace", run_id[:12], "--store", str(store_path)]) == 0
    assert views(store_path) == [("trace", run_id, "test_confirm", 1)]


def test_trace_shows_a_dev_run_without_logging(
    store_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = run_id_of(store_path, "dev")
    capsys.readouterr()
    assert main(["trace", run_id[:12], "--store", str(store_path)]) == 0
    assert views(store_path) == []


# --- lab report groups case runs by case_id -----------------------------------------------------


def test_two_cases_on_one_pair_are_two_cases_in_the_report(
    tmp_path: Path, no_guard: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """D114: 'two cases on one pair are pooled (13 runs over 12 cases)'. They are not any more."""
    config = write_config(tmp_path / "pair.yaml", "pair", SAME_PAIR)
    assert main(["run", str(config), "--store", str(tmp_path / "runs.sqlite"),
                 "--cache", str(tmp_path / "cache.sqlite"),
                 "--env-file", str(tmp_path / "no.env")]) == 0
    out = report(tmp_path / "runs.sqlite", "pair", capsys=capsys)
    assert "attack runs: n = 2 over 2 case(s)" in out
    assert "n = 2 over 1 case(s)" not in out
