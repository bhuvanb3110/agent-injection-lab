"""`lab validate-rules`, group A (PLAN_M3 step 10, D102, D117).

Each test builds a fixture repository (tests/conftest.py: a copy of the files group A reads, with
a PREREGISTRATION.md that pins them), breaks one thing, and expects exactly the check for it to
fail. No store, no network, no model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from lab.cli import main
from lab.rules.checks import (
    Check,
    check_defense_configs,
    check_deterministic_only,
    check_freeze_log,
    check_golden,
    check_nesting,
    check_pins,
    check_regeneration,
    check_sampling,
    check_schema,
    check_split,
    check_t2_strata,
    check_unpinned,
    format_checks,
    run_group_a,
)
from lab.rules.prereg import (
    PreregError,
    blinding_state,
    confirmation_state,
    fenced_blocks,
    load_pins,
    load_values,
    sha256_file,
)

WritePrereg = Callable[..., Path]
FreezeLogWriter = Any  # tests/conftest.py
DATA = "data/attack_cases"
HASH = "a" * 64  # a pin; all digits would be read as a number by YAML


def edit_json(path: Path, change: Callable[[dict[str, object]], None]) -> None:
    body = json.loads(path.read_text(encoding="utf-8"))
    change(body)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                    newline="\n")


def failing(checks: list[Check]) -> list[str]:
    return [c.name for c in checks if not c.ok]


# --- the whole of group A ---------------------------------------------------------------------


def test_group_a_passes_on_a_fixture_repo_with_a_valid_prereg(
    repo: Path, regenerated: dict[str, str]
) -> None:
    checks = run_group_a(repo, regenerated)
    assert failing(checks) == [], format_checks(checks)
    assert len(checks) == 12


def test_group_a_fails_without_a_prereg(repo: Path, regenerated: dict[str, str]) -> None:
    (repo / "PREREGISTRATION.md").unlink()
    assert failing(run_group_a(repo, regenerated)) == [
        "A1 pinned hashes", "A2 unpinned files", "A8 sampling fields match the prereg",
    ]


def test_the_real_repository_fails_group_a_until_step_11_writes_the_prereg(
    regenerated: dict[str, str],
) -> None:
    """On the checked-in files everything but the three checks that read the prereg passes, so
    step 11 has nothing else to repair."""
    from lab.rules import PROJECT_ROOT

    checks = run_group_a(PROJECT_ROOT, regenerated)
    assert not (PROJECT_ROOT / "PREREGISTRATION.md").exists()
    assert failing(checks) == [
        "A1 pinned hashes", "A2 unpinned files", "A8 sampling fields match the prereg",
    ]


def test_the_command_prints_each_check_and_exits_1_on_a_failure(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No monkeypatching: the command runs the real checks on the fixture (regeneration included).
    assert main(["validate-rules", "--root", str(repo)]) == 0
    assert "12 of 12 checks ok" in capsys.readouterr().out
    (repo / "data/attack_cases/banking_v1.jsonl").write_bytes(b"{}\n")
    assert main(["validate-rules", "--root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "FAILED  A1 pinned hashes" in out and "banking_v1.jsonl: raw bytes differ" in out


# --- A1: pinned hashes ------------------------------------------------------------------------


def test_a_tampered_pinned_file_fails_a1(repo: Path) -> None:
    path = repo / DATA / "banking_test_screen.json"
    path.write_bytes(path.read_bytes() + b" ")
    check = check_pins(repo)
    assert not check.ok
    assert check.problems == ("data/attack_cases/banking_test_screen.json: raw bytes differ from "
                              "the pin",)


def test_one_changed_line_ending_is_a_tampered_file(repo: Path) -> None:
    path = repo / "configs/policies/banking_v1.yaml"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert not check_pins(repo).ok


def test_a_missing_pinned_file_fails_a1(repo: Path) -> None:
    (repo / DATA / "banking_known_issues.json").unlink()
    assert check_pins(repo).problems == (
        "data/attack_cases/banking_known_issues.json: pinned, but the file does not exist",)


def test_the_pin_block_is_read_from_a_fenced_block_and_nothing_else(repo: Path) -> None:
    pins = load_pins(repo)
    assert pins[f"{DATA}/banking_v1.jsonl"] == sha256_file(repo / DATA / "banking_v1.jsonl")
    assert "configs/providers.yaml" not in pins  # limits are re-checked; only sampling is compared


@pytest.mark.parametrize(("text", "message"), [
    ("no blocks here", "exactly one `yaml pin-block` block; found 0"),
    ("```yaml pin-block\na/b: " + HASH + "\n```\n```yaml pin-block\nc/d: " + HASH + "\n```\n",
     "found 2"),
    ("```yaml pin-block\n../x: " + HASH + "\n```\n", "not a repository-relative path"),
    ("```yaml pin-block\n/etc/x: " + HASH + "\n```\n", "not a repository-relative path"),
    ("```yaml pin-block\na\\b: " + HASH + "\n```\n", "not a repository-relative path"),
    ("```yaml pin-block\na/b: " + "A" * 64 + "\n```\n", "not a lowercase hex SHA-256"),
    ("```yaml pin-block\na/b: abc\n```\n", "not a lowercase hex SHA-256"),
    ("```yaml pin-block\n{}\n```\n", "non-empty mapping"),
    ("```yaml pin-block\na/b: " + HASH + "\n", "never closed"),
    ("```yaml pin-block\na/b: " + "0" * 64 + "\n```\n", "quote a hash that is all digits"),
])
def test_a_malformed_pin_block_is_an_error_not_an_empty_pin_set(
    tmp_path: Path, text: str, message: str
) -> None:
    (tmp_path / "PREREGISTRATION.md").write_text(text, encoding="utf-8")
    with pytest.raises(PreregError, match=message):
        load_pins(tmp_path)


def test_a_block_with_another_info_string_is_not_the_pin_block() -> None:
    text = "```yaml\nx: 1\n```\n```python pin-block\ny\n```\n```yaml pin-block\nz: 1\n```\n"
    infos = [info for info, _ in fenced_blocks(text)]
    assert infos == ["yaml", "python pin-block", "yaml pin-block"]


# --- A2: unpinned files, the freeze log, defense configs ------------------------------------------


def test_an_unpinned_data_file_fails_a2(repo: Path) -> None:
    (repo / DATA / "notes.txt").write_text("not pinned", encoding="utf-8")
    assert check_unpinned(repo).problems == (
        "data/attack_cases/notes.txt: under data/attack_cases/ but not pinned",)


def test_an_unpinned_file_in_a_subdirectory_fails_a2_too(repo: Path) -> None:
    (repo / DATA / "extra").mkdir()
    (repo / DATA / "extra" / "more.json").write_text("{}", encoding="utf-8")
    assert not check_unpinned(repo).ok


def test_a_data_file_dropped_from_the_pin_block_fails_a2(
    repo: Path, write_prereg: WritePrereg
) -> None:
    write_prereg(repo, drop=(f"{DATA}/banking_120b.json",))
    assert check_unpinned(repo).problems == (
        "data/attack_cases/banking_120b.json: under data/attack_cases/ but not pinned",)


def test_the_golden_file_must_be_pinned(repo: Path, write_prereg: WritePrereg) -> None:
    write_prereg(repo, drop=("configs/golden/behaviour_v1.yaml",))
    assert check_unpinned(repo).problems == (
        "configs/golden/behaviour_v1.yaml: must be pinned (group A reads it) but is not",)


def write_config(root: Path, rel: str, text: str = "defense: x\n") -> str:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8", newline="\n")
    return sha256_file(root / rel)


EXPERIMENT_ON_TEST_SCREEN = {
    "name": "m4_prompt", "suite": "banking", "topology": "t2", "user_tasks": ["user_task_0"],
    "models": [{"provider": "fake", "model_id": "obedient"}], "repeats": 1, "seed": 1,
    "max_tokens": 2048, "include_benign": False, "cases": "test_screen",
    "defense": "configs/defenses/prompt.yaml",
}


def write_experiment(root: Path, **overrides: object) -> None:
    path = root / "configs/experiments/m4_prompt.yaml"
    path.write_text(yaml.safe_dump(EXPERIMENT_ON_TEST_SCREEN | overrides), encoding="utf-8")


def test_a_defense_config_unpinned_at_first_test_screen_use_fails(repo: Path) -> None:
    """An experiment on the test-screen cases names configs/defenses/prompt.yaml, and the freeze
    log has no record of it: group A fails, and with it the guard."""
    write_config(repo, "configs/defenses/prompt.yaml")
    write_experiment(repo)
    check = check_defense_configs(repo)
    assert check.problems == ("configs/experiments/m4_prompt.yaml: runs test-split cases with "
                              "configs/defenses/prompt.yaml, which is not frozen in the freeze "
                              "log",)


def test_a_frozen_defense_config_is_accepted(repo: Path, freeze_log: FreezeLogWriter) -> None:
    write_experiment(repo)
    freeze_log.write(freeze_log.freezes())
    assert check_defense_configs(repo).ok and check_freeze_log(repo).ok


def test_a_defense_config_edited_after_its_freeze_fails(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    write_experiment(repo)
    freeze_log.write(freeze_log.freezes())
    (repo / "configs/defenses/prompt.yaml").write_text("defense: edited\n", encoding="utf-8")
    assert not check_defense_configs(repo).ok
    assert check_freeze_log(repo).problems == (
        "prompt: configs/defenses/prompt.yaml changed after it was frozen",)


def test_a_draft_defense_config_used_on_dev_cases_only_needs_no_freeze(repo: Path) -> None:
    write_config(repo, "configs/defenses/prompt.yaml")
    write_experiment(repo, cases=["banking:plain_instruction:unauthorized_action:r0"])
    assert check_defense_configs(repo).ok


def test_a_defense_config_nobody_uses_is_a_draft(repo: Path) -> None:
    write_config(repo, "configs/defenses/prompt.yaml")
    assert check_defense_configs(repo).ok and check_freeze_log(repo).ok


def test_an_experiment_that_cannot_be_read_is_a_problem_not_a_pass(repo: Path) -> None:
    (repo / "configs/experiments/broken.yaml").write_text(
        "defense: configs/defenses/prompt.yaml\ncases: [no:such:case]\n", encoding="utf-8")
    write_config(repo, "configs/defenses/prompt.yaml")
    assert not check_defense_configs(repo).ok


def test_an_experiment_naming_a_defense_in_any_field_is_found(repo: Path) -> None:
    write_experiment(repo, defense=None, components={"nested": ["configs/defenses/prompt.yaml"]})
    assert not check_defense_configs(repo).ok  # no freeze log


def test_a_freeze_log_with_two_records_for_a_defense_fails(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    records = freeze_log.freezes()
    freeze_log.write([*records, records[0]])
    assert any("frozen 2 times" in p for p in check_freeze_log(repo).problems)


def test_a_freeze_of_a_missing_file_and_a_malformed_log_fail(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write([{"kind": "freeze", "defense": "prompt", "config": "configs/defenses/no.yaml",
                       "sha256": "a" * 64, "frozen_at": "2026-11-20T09:00:00Z"}])
    assert check_freeze_log(repo).problems == ("prompt: configs/defenses/no.yaml does not exist",)
    (repo / "FREEZE_LOG.yaml").write_text("entries: [nonsense]\n", encoding="utf-8")
    assert "FREEZE_LOG.yaml is malformed" in check_freeze_log(repo).problems[0]


def test_a_bare_date_is_not_a_freeze_time(repo: Path, freeze_log: FreezeLogWriter) -> None:
    records = freeze_log.freezes()
    records[0]["frozen_at"] = "2026-11-20"
    freeze_log.write(records, chain=False)
    assert "FREEZE_LOG.yaml is malformed" in check_freeze_log(repo).problems[0]


def test_an_unblinding_record_before_all_six_are_frozen_fails_a2(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write([*freeze_log.freezes(but=("everything",)), freeze_log.unblinding()])
    assert check_freeze_log(repo).problems == (
        "test_screen unblinding record: defenses not frozen: everything",)
    state = blinding_state(repo)
    assert not state.unblinded and "refused" in state.reason


def test_an_unblinding_record_dated_before_a_freeze_fails_a2(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write([*freeze_log.freezes(at="2026-12-05T00:00:00Z"), freeze_log.unblinding()])
    problems = check_freeze_log(repo).problems
    assert any("is dated before entry" in p for p in problems)  # the chain's order, too
    assert any("defenses frozen after the record" in p for p in problems)
    assert not blinding_state(repo).unblinded


def test_a_valid_unblinding_record_unblinds(repo: Path, freeze_log: FreezeLogWriter) -> None:
    assert not blinding_state(repo).unblinded  # no freeze log
    freeze_log.write(freeze_log.freezes())
    assert blinding_state(repo).reason == "FREEZE_LOG.yaml has no test_screen unblinding record"
    freeze_log.write([*freeze_log.freezes(), freeze_log.unblinding()])
    assert blinding_state(repo).unblinded and check_freeze_log(repo).ok


def test_a_frozen_config_edited_after_the_record_re_blinds(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write([*freeze_log.freezes(), freeze_log.unblinding()])
    (repo / "configs/defenses/detection.yaml").write_text("changed\n", encoding="utf-8")
    assert not blinding_state(repo).unblinded


def test_a_judge_registered_below_kappa_0_6_fails(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write([freeze_log.judge(kappa=0.59)])
    assert "below 0.6" in check_freeze_log(repo).problems[0]


# --- the hash chain ----------------------------------------------------------------------------


def valid_log(freeze_log: FreezeLogWriter) -> list[dict[str, object]]:
    return [*freeze_log.freezes(), freeze_log.unblinding(), freeze_log.judge()]


def rewrite(repo: Path, change: Callable[[list[dict[str, Any]]], None]) -> None:
    """Edit the entries of the written log in place, without re-chaining: a tamper."""
    path = repo / "FREEZE_LOG.yaml"
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(body["entries"])
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def test_a_chained_log_passes_and_the_first_entry_is_genesis(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write(valid_log(freeze_log))
    assert check_freeze_log(repo).ok
    entries = yaml.safe_load((repo / "FREEZE_LOG.yaml").read_text(encoding="utf-8"))["entries"]
    assert entries[0]["prev"] == "genesis"
    assert all(len(e["prev"]) == 64 for e in entries[1:])


def test_editing_an_earlier_entry_breaks_the_chain(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write(valid_log(freeze_log))

    def change(entries: list[dict[str, Any]]) -> None:
        entries[1]["frozen_at"] = "2026-11-19T09:00:00Z"  # backdated by a day

    rewrite(repo, change)
    problems = check_freeze_log(repo).problems
    broken = [p for p in problems if p.startswith("entry 3 (freeze): prev is ")]
    assert len(broken) == 1 and "edited, removed or reordered" in broken[0]
    state = blinding_state(repo)
    assert not state.unblinded and "chain is broken" in state.reason


def test_deleting_an_earlier_entry_breaks_the_chain(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write(valid_log(freeze_log))
    rewrite(repo, lambda entries: entries.pop(2))
    problems = check_freeze_log(repo).problems
    assert any(p.startswith("entry 3 (freeze): prev is ") for p in problems)


def test_reordering_entries_breaks_the_chain(repo: Path, freeze_log: FreezeLogWriter) -> None:
    freeze_log.write(valid_log(freeze_log))

    def change(entries: list[dict[str, Any]]) -> None:
        entries[1], entries[2] = entries[2], entries[1]

    rewrite(repo, change)
    assert not check_freeze_log(repo).ok


def test_editing_the_first_entry_is_caught_by_the_second(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write(valid_log(freeze_log))

    def change(entries: list[dict[str, Any]]) -> None:
        entries[0]["defense"] = "detection"  # still a valid-looking entry on its own
        entries[0]["config"] = "configs/defenses/detection.yaml"

    rewrite(repo, change)
    assert any(p.startswith("entry 2 (freeze): prev is ") for p in check_freeze_log(repo).problems)


def test_a_wrong_first_prev_is_caught(repo: Path, freeze_log: FreezeLogWriter) -> None:
    freeze_log.write(valid_log(freeze_log))
    rewrite(repo, lambda entries: entries[0].update(prev="b" * 64))
    assert check_freeze_log(repo).problems[0].startswith("entry 1 (freeze): prev is ")


def test_the_chain_does_not_depend_on_how_the_yaml_is_written(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write(valid_log(freeze_log))
    path = repo / "FREEZE_LOG.yaml"
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump(body, sort_keys=True, default_flow_style=True),
                    encoding="utf-8")
    assert check_freeze_log(repo).ok  # reformatted, same content


def test_the_last_entry_is_the_one_the_chain_cannot_vouch_for(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    """Documented limit: nothing follows the last entry, so editing it is for git to show."""
    freeze_log.write(valid_log(freeze_log))
    rewrite(repo, lambda entries: entries[-1].update(kappa=0.9))
    assert check_freeze_log(repo).ok


# --- the M6 confirmation record ---------------


def test_a_confirmation_after_the_screen_unblinding_is_valid(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write(freeze_log.full())
    assert check_freeze_log(repo).ok
    assert confirmation_state(repo) == (True, "")


def test_a_confirmation_before_the_screen_unblinding_is_refused(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write([*freeze_log.freezes(), freeze_log.confirmation(at="2026-11-25T00:00:00Z"),
                      freeze_log.unblinding()])
    assert any(p.startswith("confirmation: ") for p in check_freeze_log(repo).problems)
    assert not confirmation_state(repo)[0]


def test_a_confirmation_without_any_unblinding_is_refused(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write([*freeze_log.freezes(), freeze_log.confirmation()])
    assert not confirmation_state(repo)[0]
    assert "not validly unblinded" in confirmation_state(repo)[1]


def test_a_changed_selection_file_voids_the_confirmation(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write(freeze_log.full())
    (repo / "configs/m6_selection.yaml").write_text("confirm: [t3]\n", encoding="utf-8")
    assert "changed after it was registered" in " ".join(check_freeze_log(repo).problems)
    assert not confirmation_state(repo)[0]


def test_a_confirm_unblinding_needs_the_confirmation_first(
    repo: Path, freeze_log: FreezeLogWriter
) -> None:
    freeze_log.write([*freeze_log.freezes(), freeze_log.unblinding(),
                      freeze_log.unblinding(at="2026-12-09T00:00:00Z", scope="test_confirm")])
    assert "there is no confirmation registration" in " ".join(check_freeze_log(repo).problems)
    assert not blinding_state(repo, "test_confirm").unblinded
    freeze_log.write([*freeze_log.full(),
                      freeze_log.unblinding(at="2026-12-20T00:00:00Z", scope="test_confirm")])
    assert check_freeze_log(repo).ok
    assert blinding_state(repo, "test_confirm").unblinded


# --- A3, A9: schema and strata ------------------------------------------------------------------


def edit_rows(root: Path, change: Callable[[list[dict[str, object]]], None]) -> None:
    path = root / DATA / "banking_v1.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    change(rows)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8",
                    newline="\n")


def test_a_duplicate_case_id_fails_a3(repo: Path) -> None:
    edit_rows(repo, lambda rows: rows.append(dict(rows[0])))
    check = check_schema(repo)
    assert any("appears 2 times" in p for p in check.problems)


def test_an_unregistered_predicate_fails_a3(repo: Path) -> None:
    def change(rows: list[dict[str, object]]) -> None:
        rows[-1]["check"] = "lab_made_up"

    edit_rows(repo, change)
    assert not check_schema(repo).ok


def test_a_hand_edited_stratum_fails_a9_and_a3(repo: Path) -> None:
    def change(rows: list[dict[str, object]]) -> None:
        row = next(r for r in rows if r["t2_stratum"] == "local")
        row["t2_stratum"] = "cross_worker"

    edit_rows(repo, change)
    check = check_t2_strata(repo)
    assert len(check.problems) == 1 and "t2_stratum is 'cross_worker', WORKER_TOOLS gives" in (
        check.problems[0])
    assert not check_schema(repo).ok


def test_a_wrong_hop_path_fails_a9(repo: Path) -> None:
    def change(rows: list[dict[str, object]]) -> None:
        rows[0]["required_hops"] = ["payments"]

    edit_rows(repo, change)
    assert "required_hops" in check_t2_strata(repo).problems[0]


# --- A4: the split ------------------------------------------------------------------------------


def test_a_pair_in_both_splits_fails_a4(repo: Path) -> None:
    def change(body: dict[str, object]) -> None:
        cases = body["cases"]
        assert isinstance(cases, dict)
        case = next(c for c, s in cases.items() if s == "test")
        cases[case] = "dev"  # its pair stays test, so the pair is now in both splits

    edit_json(repo / DATA / "banking_split.json", change)
    problems = check_split(repo).problems
    assert len(problems) == 1 and "a pair would be in both splits" in problems[0]


def test_a_case_missing_from_the_split_and_a_stray_one_fail_a4(repo: Path) -> None:
    def change(body: dict[str, object]) -> None:
        cases = body["cases"]
        assert isinstance(cases, dict)
        del cases[next(iter(cases))]
        cases["banking:not:a:case"] = "dev"

    edit_json(repo / DATA / "banking_split.json", change)
    problems = check_split(repo).problems
    assert any("a case with no split" in p for p in problems)
    assert any("in the split but not a case" in p for p in problems)


def test_a_case_listed_twice_in_the_split_file_fails_a4(repo: Path) -> None:
    path = repo / DATA / "banking_split.json"
    text = path.read_text(encoding="utf-8")
    first = next(line for line in text.splitlines() if '"banking:' in line and '": "' in line)
    path.write_text(text.replace(first, first + "\n" + first, 1), encoding="utf-8", newline="\n")
    assert "repeated key" in check_split(repo).problems[0]


def test_the_d84_pair_must_be_dev(repo: Path) -> None:
    def change(body: dict[str, object]) -> None:
        pairs = body["pairs"]
        assert isinstance(pairs, dict)
        pairs["user_task_2|injection_task_4"] = "test"

    edit_json(repo / DATA / "banking_split.json", change)
    assert any("user_task_2|injection_task_4 must be dev" in p for p in check_split(repo).problems)


def test_a_dev_and_a_test_wording_that_share_four_words_fail_a4(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lab.attacks import techniques

    shared = "do exactly as written here"
    plain = techniques.TEMPLATES["plain_instruction"]["default"]
    monkeypatch.setitem(techniques.TEMPLATES["plain_instruction"], "default", {
        "dev": f"{plain['dev']} {shared}", "test": f"{plain['test']} {shared}"})
    problems = check_split(repo).problems
    assert any("share" in p and "do exactly as written" in p for p in problems)


# --- A5: nesting ------------------------------------------------------------------------------


def test_a_test_screen_case_that_is_not_a_test_case_fails_a5(repo: Path) -> None:
    dev_case = next(c for c, s in json.loads(
        (repo / DATA / "banking_split.json").read_text(encoding="utf-8"))["cases"].items()
        if s == "dev")

    def change(body: dict[str, object]) -> None:
        ids = body["ids"]
        assert isinstance(ids, list)
        ids[0] = dev_case

    edit_json(repo / DATA / "banking_test_screen.json", change)
    assert any(p.startswith("test_screen is not inside test") for p in check_nesting(repo).problems)


def test_a_120b_case_outside_test_screen_fails_a5(repo: Path) -> None:
    screen = set(json.loads((repo / DATA / "banking_test_screen.json").read_text(
        encoding="utf-8"))["ids"])
    outsider = next(c for c, s in json.loads(
        (repo / DATA / "banking_split.json").read_text(encoding="utf-8"))["cases"].items()
        if s == "test" and c not in screen)

    def change(body: dict[str, object]) -> None:
        ids = body["ids"]
        assert isinstance(ids, list)
        ids[0] = outsider

    edit_json(repo / DATA / "banking_120b.json", change)
    assert any(p.startswith("120b is not inside test_screen") for p in check_nesting(repo).problems)


def test_the_repeat_subset_must_have_its_declared_share_inside_120b(repo: Path) -> None:
    def change(body: dict[str, object]) -> None:
        body["per_group"] = 4

    edit_json(repo / DATA / "banking_repeat.json", change)
    assert any("expected 4" in p for p in check_nesting(repo).problems)


def test_a_subset_with_the_wrong_size_or_a_repeated_case_fails_a5(repo: Path) -> None:
    def change(body: dict[str, object]) -> None:
        ids = body["ids"]
        assert isinstance(ids, list)
        ids.append(ids[0])

    edit_json(repo / DATA / "banking_120b.json", change)
    problems = check_nesting(repo).problems
    assert any("listed twice" in p for p in problems) and any("its n is" in p for p in problems)


# --- A6: regeneration -------------------------------------------------------------------------


def test_a_regeneration_mismatch_fails_a6_even_when_the_file_is_re_pinned(
    repo: Path, regenerated: dict[str, str], write_prereg: WritePrereg
) -> None:
    """The pin hashes agree with the edited file, so A1 is green; only the scripts can tell."""
    def change(body: dict[str, object]) -> None:
        body["ids"] = list(reversed(body["ids"]))  # type: ignore[call-overload]

    edit_json(repo / DATA / "banking_repeat.json", change)
    write_prereg(repo)
    assert check_pins(repo).ok
    check = check_regeneration(repo, regenerated)
    assert check.problems == ("banking_repeat.json: differs from the regenerated file",)


def test_a_missing_committed_file_fails_a6(repo: Path, regenerated: dict[str, str]) -> None:
    (repo / DATA / "banking_split.json").unlink()
    assert check_regeneration(repo, regenerated).problems == ("banking_split.json: missing",)


def test_an_edited_composition_fails_a6(repo: Path, regenerated: dict[str, str]) -> None:
    """Changing a seed in the composition file changes what the scripts write; the committed
    files then no longer match. (Run for real: this is the one test that regenerates.)"""
    path = repo / DATA / "banking_composition.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("20261028:banking:pairs",
                                                            "20261028:banking:pairz", 1),
                    encoding="utf-8", newline="\n")
    check = check_regeneration(repo)
    assert not check.ok and any("differs from the regenerated file" in p for p in check.problems)


# --- A7, A8, A10 ------------------------------------------------------------------------------


def test_a_rule_that_changes_behaviour_fails_a7(monkeypatch: pytest.MonkeyPatch) -> None:
    from lab.rules import PROJECT_ROOT
    from lab.scoring import attack_metrics

    assert check_golden(PROJECT_ROOT).ok
    monkeypatch.setattr(attack_metrics, "REQUEST_PHRASES", ("let me know",))
    problems = check_golden(PROJECT_ROOT).problems
    assert any("requested_user_input" in p and "please confirm" not in p for p in problems)
    assert any("PLEASE CONFIRM" in p for p in problems)


def test_a_changed_normalise_fails_a7(monkeypatch: pytest.MonkeyPatch) -> None:
    from lab.rules import PROJECT_ROOT
    from lab.tracing import markers

    monkeypatch.setattr(markers, "normalise", lambda text: "".join(text.casefold().split()))
    assert any(p.startswith("normalise:") for p in check_golden(PROJECT_ROOT).problems)


def test_a_changed_attacker_effect_fails_a7(monkeypatch: pytest.MonkeyPatch) -> None:
    from lab.rules import PROJECT_ROOT
    from lab.scoring import attacker_effect

    monkeypatch.setattr(attacker_effect, "attacker_effect", lambda pre, post: False)
    assert any(p.startswith("attacker_effect:") for p in check_golden(PROJECT_ROOT).problems)


def test_a_changed_sabotage_constant_fails_a7(monkeypatch: pytest.MonkeyPatch) -> None:
    from lab.attacks import predicates
    from lab.rules import PROJECT_ROOT

    monkeypatch.setattr(predicates, "SABOTAGE_AMOUNT", 1234.56)
    problems = check_golden(PROJECT_ROOT).problems
    assert any("SABOTAGE_AMOUNT is 1234.56, frozen" in p for p in problems)


def test_a_changed_summary_parser_fails_a7(monkeypatch: pytest.MonkeyPatch) -> None:
    from lab.defenses import human
    from lab.rules import PROJECT_ROOT

    real = human.load_summary_parser()
    looser = real.model_copy(update={"name": "summary_parser_v2"})
    monkeypatch.setattr(human, "load_summary_parser", lambda: looser)
    assert any("digest" in p for p in check_golden(PROJECT_ROOT).problems)


def test_a_missing_or_malformed_golden_file_fails_a7(repo: Path) -> None:
    (repo / "configs/golden/behaviour_v1.yaml").write_text("version: 1\n", encoding="utf-8")
    assert "is malformed" in check_golden(repo).problems[0]
    (repo / "configs/golden/behaviour_v1.yaml").unlink()
    assert check_golden(repo).problems == ("configs/golden/behaviour_v1.yaml does not exist",)


def test_a_golden_file_weakened_to_match_a_changed_rule_fails_a1(repo: Path) -> None:
    path = repo / "configs/golden/behaviour_v1.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace('expected: "abc"', 'expected: "ABC"'),
                    encoding="utf-8", newline="\n")
    assert check_pins(repo).problems == (
        "configs/golden/behaviour_v1.yaml: raw bytes differ from the pin",)


def test_a_sampling_change_in_providers_yaml_fails_a8(repo: Path) -> None:
    path = repo / "configs/providers.yaml"
    text = path.read_text(encoding="utf-8").replace("temperature: 0.6", "temperature: 0.7")
    path.write_text(text, encoding="utf-8", newline="\n")
    problems = check_sampling(repo).problems
    assert len(problems) == 2 and all("temperature is 0.7 in providers.yaml, 0.6" in p
                                      for p in problems)


def test_a_model_with_sampling_but_not_in_the_prereg_fails_a8(repo: Path) -> None:
    path = repo / "configs/providers.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("      qwen3:8b:\n        tool_calling: true\n",
                                 "      qwen3:8b:\n        tool_calling: true\n"
                                 "        sampling: {temperature: 0.1, reasoning_effort: null,"
                                 " source: x, checked: 2026-10-25}\n"),
                    encoding="utf-8", newline="\n")
    assert check_sampling(repo).problems == (
        "ollama/qwen3:8b: has sampling in providers.yaml, none in the prereg",)


def test_the_values_block_is_read(repo: Path) -> None:
    values = load_values(repo)
    assert set(values.sampling) == {"groq/openai/gpt-oss-120b", "groq/openai/gpt-oss-20b"}


def test_deterministic_only_is_t4(monkeypatch: pytest.MonkeyPatch) -> None:
    from lab.topologies import presets

    assert check_deterministic_only().ok
    monkeypatch.setitem(presets.PRESETS, "t4", presets.Preset("planner_workers", (), parent="t2"))
    assert not check_deterministic_only().ok


def test_a_policy_with_an_amount_cap_is_no_longer_t4(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import create_model

    from lab.defenses import policy

    capped = create_model("Policy", __base__=policy.Policy, max_amount=(float, 100.0))
    monkeypatch.setattr(policy, "Policy", capped)
    assert any("amount" in p for p in check_deterministic_only().problems)
