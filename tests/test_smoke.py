"""End to end: `lab run configs/experiments/smoke.yaml`, then `lab report smoke`, on FakeModels.

This is the CI smoke run (D8). It goes through the real CLI, config expansion, scheduler,
limiter, cache, LabLLM, T1 and run_case, and checks that two fresh runs store identical results.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from lab.cli import main

SMOKE = Path(__file__).resolve().parents[1] / "configs" / "experiments" / "smoke.yaml"


def lab_run(workdir: Path) -> int:
    return main([
        "run", str(SMOKE),
        "--store", str(workdir / "runs.sqlite"),
        "--cache", str(workdir / "cache.sqlite"),
        "--env-file", str(workdir / "no.env"),
    ])


def stored_results(store: Path) -> list[tuple[object, ...]]:
    """Everything about each run that must not depend on when or how fast it ran."""
    db = sqlite3.connect(store)
    rows = db.execute(
        "SELECT run_id, model_id, user_task_id, seed, outcome, outcome_subtype, utility,"
        " state_check_passed, n_calls, input_tokens, output_tokens, malformed_tool_calls,"
        " messages FROM runs ORDER BY run_id"
    ).fetchall()
    # Each call's request key is a hash of exactly what was sent, so equal keys mean the two
    # runs sent identical requests.
    keys = [
        (run_id, idx, json.loads(record)["request_key"])
        for run_id, idx, record in db.execute(
            "SELECT run_id, idx, record FROM calls ORDER BY run_id, idx"
        )
    ]
    db.close()
    return [*rows, *keys]


def test_smoke_runs_end_to_end_and_is_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    assert lab_run(first) == 0
    out = capsys.readouterr().out
    assert "6 runs, 0 already done, 6 to go" in out
    assert "All runs done" in out

    assert lab_run(second) == 0
    assert stored_results(first / "runs.sqlite") == stored_results(second / "runs.sqlite")

    db = sqlite3.connect(first / "runs.sqlite")
    outcomes = dict(
        db.execute("SELECT model_id, group_concat(DISTINCT outcome) FROM runs GROUP BY model_id")
    )
    db.close()
    assert outcomes == {"ground-truth": "solved", "refuse": "not_solved"}

    capsys.readouterr()
    assert main(["report", "smoke", "--store", str(first / "runs.sqlite"),
                 "--config", str(SMOKE)]) == 0
    report = capsys.readouterr().out
    assert "Runs stored: 6 of 6 (0 pending, unscored)" in report
    assert "benign utility   3/3 = 100.0%  (95% Wilson CI 43.9% to 100.0%)" in report
    assert "benign utility   0/3 = 0.0%  (95% Wilson CI 0.0% to 56.1%)" in report


def test_rerun_resumes_and_makes_no_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lab_run(tmp_path) == 0
    capsys.readouterr()
    assert lab_run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "6 runs, 6 already done, 0 to go" in out
    assert "Done this invocation: 0 stored" in out
    db = sqlite3.connect(tmp_path / "runs.sqlite")
    assert db.execute("SELECT count(*) FROM requests").fetchone() == (12,)  # first run only
    db.close()


def test_missing_key_stops_before_any_run_and_names_only_the_variable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    baseline = SMOKE.with_name("baseline_banking.yaml")
    code = main(["run", str(baseline), "--store", str(tmp_path / "runs.sqlite"),
                 "--cache", str(tmp_path / "cache.sqlite"), "--env-file", str(tmp_path / "no")])
    assert code == 2
    assert "GROQ_API_KEY is not set" in capsys.readouterr().err
    db = sqlite3.connect(tmp_path / "runs.sqlite")
    assert db.execute("SELECT count(*) FROM requests").fetchone() == (0,)
    db.close()
