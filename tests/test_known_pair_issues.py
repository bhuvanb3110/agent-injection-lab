"""`lab report` prints a pair's known issue whenever the report covers that pair (D84, D91)."""

import contextlib
import io
from pathlib import Path

import pytest
import yaml

from lab.cli import main, report_text
from lab.domains.agentdojo_bridge import KNOWN_PAIR_ISSUES

D84_PAIR = ("user_task_2", "injection_task_4")


def test_report_text_prints_a_covered_pairs_note() -> None:
    text = report_text("e", [], 0, None, pairs={D84_PAIR})
    assert f"Known issue, user_task_2 x injection_task_4: {KNOWN_PAIR_ISSUES[D84_PAIR]}" in text
    assert "D84" in text and "Scored unchanged." in text


def test_report_text_is_silent_when_the_pair_is_not_covered() -> None:
    other = {("user_task_2", "injection_task_5"), ("user_task_4", "injection_task_4")}
    assert "Known issue" not in report_text("e", [], 0, None, pairs=other)
    assert "Known issue" not in report_text("e", [], 0, None)


def test_a_user_task_issue_and_a_pair_issue_are_both_printed() -> None:
    text = report_text("e", [], 0, None, tasks={"user_task_9"}, pairs={D84_PAIR})
    assert "Known issue, user_task_9:" in text
    assert "Known issue, user_task_2 x injection_task_4:" in text


def run_and_report(tmp_path: Path, name: str, injection_task: str) -> str:
    config = tmp_path / f"{name}.yaml"
    config.write_text(yaml.safe_dump({
        "name": name, "suite": "banking", "topology": "t1", "user_tasks": ["user_task_2"],
        "models": [{"provider": "fake", "model_id": "ground-truth"}], "repeats": 1, "seed": 7,
        "max_tokens": 2048, "attack": {"injection_tasks": [injection_task]},
    }))
    store, cache = tmp_path / "runs.sqlite", tmp_path / "cache.sqlite"
    assert main(["run", str(config), "--store", str(store), "--cache", str(cache),
                 "--env-file", str(tmp_path / "no.env")]) == 0
    return report_to_text(store, config, name)


def report_to_text(store: Path, config: Path, name: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["report", name, "--store", str(store), "--config", str(config)]) == 0
    return out.getvalue()


@pytest.mark.usefixtures("no_guard")
def test_lab_report_prints_it_for_stored_runs_of_the_pair_only(tmp_path: Path) -> None:
    assert "Known issue, user_task_2 x injection_task_4:" in run_and_report(
        tmp_path, "pair", "injection_task_4"
    )
    # The same user task attacked with another injection task is not that pair.
    assert "x injection_task" not in run_and_report(tmp_path, "other", "injection_task_5")
