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
    assert "8 runs, 0 already done, 8 to go" in out
    assert "All runs done" in out

    assert lab_run(second) == 0
    assert stored_results(first / "runs.sqlite") == stored_results(second / "runs.sqlite")

    db = sqlite3.connect(first / "runs.sqlite")
    outcomes = set(db.execute("SELECT model_id, user_task_id, outcome FROM runs"))
    db.close()
    tasks = ["user_task_0", "user_task_1", "user_task_2", "user_task_8"]
    assert outcomes == {
        *(("ground-truth", t, "solved") for t in tasks),
        *(("refuse", t, "not_solved") for t in tasks[:3]),
        ("refuse", "user_task_8", "solved"),  # passes with no action taken (D37)
    }

    capsys.readouterr()
    assert main(["report", "smoke", "--store", str(first / "runs.sqlite"),
                 "--config", str(SMOKE)]) == 0
    report = capsys.readouterr().out
    assert "Runs stored: 8 of 8 (0 pending, unscored)" in report
    ground_truth, refuse = report.split("fake/ground-truth")[1].split("fake/refuse")
    assert "discriminating tasks  3/3 = 100.0%  (43.9% to 100.0%)  headline" in ground_truth
    assert "all tasks             4/4 = 100.0%  (51.0% to 100.0%)" in ground_truth
    assert "do-nothing floor      1/4 = 25.0%  (4.6% to 69.9%)  same runs" in ground_truth
    # The scripted do-nothing model scores exactly the floor, and 0 on the headline.
    assert "discriminating tasks  0/3 = 0.0%  (0.0% to 56.1%)  headline" in refuse
    assert "all tasks             1/4 = 25.0%  (4.6% to 69.9%)" in refuse
    assert "do-nothing floor      1/4 = 25.0%  (4.6% to 69.9%)  same runs" in refuse


def test_rerun_resumes_and_makes_no_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lab_run(tmp_path) == 0
    capsys.readouterr()
    assert lab_run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "8 runs, 8 already done, 0 to go" in out
    assert "Done this invocation: 0 stored" in out
    db = sqlite3.connect(tmp_path / "runs.sqlite")
    assert db.execute("SELECT count(*) FROM requests").fetchone() == (15,)  # first run only
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
