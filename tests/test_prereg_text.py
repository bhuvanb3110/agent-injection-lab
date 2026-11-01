"""PREREGISTRATION.md says what it was written to say (PLAN_M3 step 11).

`validate-rules` checks the machine-readable blocks. This checks the prose that cannot be checked
by a hash alone: the hypotheses are the brief's, word for word; Appendix A is the pinned rubric;
the file has exactly the two blocks the parser reads; and the numbers it quotes about the drawn
subsets are the subset files' own.
"""

import json
from pathlib import Path

from lab.rules.prereg import fenced_blocks, load_pins, load_values

ROOT = Path(__file__).resolve().parents[1]
PREREG = (ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")
DATA = ROOT / "data" / "attack_cases"


def brief_rows() -> dict[str, list[str]]:
    """BRIEF §2's table: RQ id -> [question, starting hypothesis]."""
    text = (ROOT / "docs" / "BRIEF.md").read_text(encoding="utf-8")
    section = text.split("## 2. Project summary", 1)[1].split("## 3.", 1)[0]
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) == 3 and cells[0].startswith("RQ"):
            rows[cells[0]] = cells[1:]
    return rows


def test_the_hypotheses_are_the_briefs_word_for_word() -> None:
    rows = brief_rows()
    assert sorted(rows) == ["RQ1", "RQ2", "RQ3", "RQ4", "RQ5", "RQ6 (stretch)"]
    for rq in ("RQ1", "RQ2", "RQ3", "RQ4", "RQ5"):
        question, hypothesis = rows[rq]
        assert f"| {rq} | {question} | {hypothesis} |" in PREREG, rq


def test_the_file_has_exactly_the_two_blocks_the_parser_reads() -> None:
    assert [info for info, _ in fenced_blocks(PREREG)] == ["yaml pin-block", "yaml prereg-values"]


def test_appendix_a_is_the_pinned_rubric() -> None:
    rubric = (ROOT / "configs" / "judge" / "rubric_v1.md").read_text(encoding="utf-8").rstrip("\n")
    assert rubric in PREREG
    assert "configs/judge/rubric_v1.md" in load_pins(ROOT)


def test_the_m4_configs_the_prereg_describes_are_pinned() -> None:
    pins = load_pins(ROOT)
    for name in ("m4_topology_20b", "m4_topology_20b_repeats", "m4_topology_120b",
                 "m4_topology_120b_repeats", "m4_benign_20b", "m4_benign_120b"):
        assert f"configs/experiments/{name}.yaml" in pins, name
        assert f"`{name}`" in PREREG, name
    assert set(load_values(ROOT).sampling) == {"groq/openai/gpt-oss-120b",
                                               "groq/openai/gpt-oss-20b"}


def test_the_counts_quoted_in_section_3_are_the_subset_files() -> None:
    def marginals(subset: str) -> dict[str, dict[str, int]]:
        return json.loads((DATA / f"banking_{subset}.json").read_text(encoding="utf-8"))[
            "marginals"]

    screen, b120, repeat = marginals("test_screen"), marginals("120b"), marginals("repeat")
    assert screen["technique"]["important_instructions"] == 8
    assert "holds 8 `important_instructions` cases" in PREREG

    def row(m: dict[str, dict[str, int]]) -> tuple[str, str]:
        c, g = m["channel"], m["goal"]
        return (
            f"{c['tool_output']} / {c['file']} / {c['tool_description']} / {c['multi']}",
            f"{g['unauthorized_action']} / {g['exfiltration']} / {g['sabotage']} / "
            f"{g['propagation']}",
        )

    for label, m in (("Test-screen 50", screen), ("120b subset 25", b120),
                     ("Repeat subset 10", repeat)):
        channel, goal = row(m)
        assert f"| {label} | {channel} | {goal} |" in PREREG, label
    split = json.loads((DATA / "banking_split.json").read_text(encoding="utf-8"))["cases"]
    assert sum(v == "dev" for v in split.values()) == 68
    assert sum(v == "test" for v in split.values()) == 157
    test_confirm = [i for i, v in split.items() if v == "test"
                    and i not in set(json.loads((DATA / "banking_test_screen.json").read_text(
                        encoding="utf-8"))["ids"])]
    assert len(test_confirm) == 107
    assert sum(i.startswith("banking:important_instructions:") for i in test_confirm) == 95
