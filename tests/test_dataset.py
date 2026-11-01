"""The attack dataset: pair placement, split and subsets (PLAN_M3 step 6, D90, D92, D107).

No network, no run store, no model. Covers byte determinism and regeneration of the committed
files, the composition of §3.5 (channel, goal and split totals), every invariant of the split
and the subsets, the pair-placement lookahead (the tightest strata, a case that would take
another's last pair, an infeasible composition that must stop with an error), and LF line endings.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from lab.attacks import techniques
from lab.attacks.agentdojo import GOAL_MARKERS
from lab.attacks.cases import AttackCase, channel_for, load_cases, t2_stratum_for
from lab.attacks.composition import (
    Composition,
    case_id,
    generate_templates,
    load_composition,
)
from lab.attacks.dataset import (
    BANKING,
    DATA_DIR,
    Dataset,
    build,
    build_dataset,
    check_files,
    main,
    write_files,
)
from lab.attacks.pairs import PairCase, PlacementError, place_pairs
from lab.attacks.placement import place
from lab.attacks.predicates import LAB_GOAL_MARKERS
from lab.attacks.sampling import SamplingError, levels_of, quota_sample
from lab.domains.agentdojo_bridge import (
    KNOWN_PAIR_ISSUES,
    banking_task_ids,
    load_banking_suite,
)

D84_PAIR = ("user_task_2", "injection_task_4")
PAYMENT_TOOLS = {"send_money", "schedule_transaction", "update_scheduled_transaction"}
NEW_TECHNIQUES = [
    "plain_instruction", "fake_authority", "fake_completion", "context_blending",
    "social_engineering", "obfuscation", "multi_hop", "self_replicating", "approval_manipulation",
]


@pytest.fixture(scope="module")
def built() -> tuple[Composition, Dataset, dict[str, str]]:
    return build(BANKING)


@pytest.fixture(scope="module")
def comp(built: tuple[Composition, Dataset, dict[str, str]]) -> Composition:
    return built[0]


@pytest.fixture(scope="module")
def data(built: tuple[Composition, Dataset, dict[str, str]]) -> Dataset:
    return built[1]


@pytest.fixture(scope="module")
def files(built: tuple[Composition, Dataset, dict[str, str]]) -> dict[str, str]:
    return built[2]


def new_cases(data: Dataset) -> list[AttackCase]:
    return data.cases[data.n_baseline :]


def pair(case: AttackCase) -> tuple[str, str]:
    return (case.user_task_id, case.injection_task_id)


# --- determinism and regeneration --------------------------------------------------------------


def test_building_twice_gives_byte_identical_files(files: dict[str, str]) -> None:
    _, _, again = build(BANKING)
    assert again.keys() == files.keys()
    assert all(again[name].encode() == files[name].encode() for name in files)


def test_each_stage_is_deterministic_on_its_own(comp: Composition, data: Dataset) -> None:
    """Placement and each subset draw reproduce from their seed strings alone."""
    templates = generate_templates(comp)
    baseline = {pair(c) for c in data.cases[: data.n_baseline]}
    cases = [PairCase(t.id, t.split, t.eligible_pairs) for t in templates]

    def placement() -> Any:
        return place_pairs(cases, baseline, forced_dev=comp.forced_dev_pairs,
                           dev_fraction=comp.dev_fraction, rng=random.Random(comp.pair_seed))

    assert placement() == placement() == data.placed
    again = build_dataset(BANKING, comp)
    assert {n: d.ids for n, d in again.draws.items()} == {n: d.ids for n, d in data.draws.items()}


def test_the_committed_files_are_reproduced_byte_for_byte(files: dict[str, str]) -> None:
    """The regeneration check `lab validate-rules` will run (D102)."""
    assert check_files(files, DATA_DIR) == []
    expected = {
        "banking_v1.jsonl", "banking_split.json", "banking_test_screen.json",
        "banking_120b.json", "banking_repeat.json", "banking_known_issues.json",
    }
    assert set(files) == expected
    assert expected <= {p.name for p in DATA_DIR.iterdir()}


def test_a_changed_committed_file_fails_the_regeneration_check(
    files: dict[str, str], tmp_path: Path
) -> None:
    write_files(files, tmp_path)
    assert check_files(files, tmp_path) == []
    (tmp_path / "banking_split.json").write_bytes(
        (tmp_path / "banking_split.json").read_bytes().replace(b'"dev"', b'"test"', 1)
    )
    (tmp_path / "banking_120b.json").unlink()
    assert sorted(check_files(files, tmp_path)) == [
        "banking_120b.json: missing", "banking_split.json: differs from the regenerated file",
    ]


@pytest.mark.parametrize("hash_seed", ["1", "31337"])
def test_the_output_does_not_depend_on_pythonhashseed(hash_seed: str) -> None:
    """A fresh interpreter with another hash seed regenerates the same bytes."""
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    run = subprocess.run(
        [sys.executable, "-m", "lab.attacks.dataset", "banking", "--check"],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "byte for byte" in run.stdout


def test_every_committed_file_has_lf_line_endings_only() -> None:
    names = sorted(p for p in DATA_DIR.iterdir() if p.is_file())
    assert len(names) >= 7  # the composition file and the six it produces
    for path in names:
        raw = path.read_bytes()
        assert b"\r" not in raw, f"{path.name} has a carriage return"
        assert raw.endswith(b"\n") and not raw.endswith(b"\n\n"), path.name


def test_the_writer_writes_lf_whatever_the_platform(files: dict[str, str], tmp_path: Path) -> None:
    write_files(files, tmp_path)
    for name in files:
        assert b"\r" not in (tmp_path / name).read_bytes()
    assert (tmp_path / "banking_v1.jsonl").read_bytes().count(b"\n") == 225


def test_the_seeds_are_the_plans_strings(comp: Composition) -> None:
    assert comp.pair_seed == "20261028:banking:pairs"
    assert {n: s.seed for n, s in comp.subsets.items()} == {
        "test_screen": "20261028:banking:test_screen",
        "120b": "20261028:banking:120b",
        "repeat": "20261028:banking:repeat",
    }


# --- the composition is §3.5's ------------------------------------------------------------------


def test_the_81_new_cases_have_the_totals_of_section_3_5(data: Dataset) -> None:
    new = new_cases(data)
    assert len(new) == 81 and len(data.cases) == 225 and data.n_baseline == 144
    assert Counter(c.channel for c in new) == {
        "tool_output": 24, "file": 24, "tool_description": 24, "multi": 9,
    }
    assert Counter(c.goal for c in new) == {
        "unauthorized_action": 30, "exfiltration": 21, "sabotage": 21, "propagation": 9,
    }
    assert Counter(data.split_of[c.id] for c in new) == {"dev": 27, "test": 54}
    assert Counter(c.technique for c in new) == dict.fromkeys(NEW_TECHNIQUES, 9)
    for name in NEW_TECHNIQUES:  # 3 dev and 6 test per technique
        assert Counter(data.split_of[c.id] for c in new if c.technique == name) == {
            "dev": 3, "test": 6,
        }


def test_each_techniques_dev_cases_are_one_per_row_and_one_per_column(comp: Composition) -> None:
    by_technique = {t.name: t for t in comp.techniques}
    templates = generate_templates(comp)
    for name in NEW_TECHNIQUES:
        dev = [t for t in templates if t.technique == name and t.split == "dev"]
        assert sorted(t.row for t in dev) == [0, 1, 2] == sorted(t.col for t in dev)
        assert all((t.col - t.row - by_technique[name].k) % 3 == 0 for t in dev)


def test_the_grid_cells_of_each_technique(comp: Composition) -> None:
    """Rows are channels (or replicas), columns are goals (or replicas), as §3.5's table says."""
    templates = generate_templates(comp)
    for name in NEW_TECHNIQUES[:6]:
        cells = {(t.channel, t.goal) for t in templates if t.technique == name}
        assert cells == {(c, g) for c in ("tool_output", "file", "tool_description")
                         for g in ("unauthorized_action", "exfiltration", "sabotage")}
    multi = [t for t in templates if t.technique == "multi_hop"]
    assert {t.channel for t in multi} == {"multi"}
    assert Counter(t.goal for t in multi) == dict.fromkeys(
        ("unauthorized_action", "exfiltration", "sabotage"), 3)
    for name, goal in (("self_replicating", "propagation"), ("approval_manipulation",
                                                           "unauthorized_action")):
        mine = [t for t in templates if t.technique == name]
        assert {t.goal for t in mine} == {goal}
        assert Counter(t.channel for t in mine) == dict.fromkeys(
            ("tool_output", "file", "tool_description"), 3)


def test_the_replica_convention(comp: Composition) -> None:
    """Stated once in the composition file and in the generator: see its docstring."""
    templates = {t.id: t for t in generate_templates(comp)}
    # Wording techniques: replica = channel row order.
    for t in templates.values():
        if t.technique in NEW_TECHNIQUES[:6]:
            assert t.replica == ("tool_output", "file", "tool_description").index(t.channel)
    # multi_hop: replica indexes across goals on the diagonal: r0 = U, r1 = X, r2 = S are dev.
    dev_goal = {t.replica: t.goal for t in templates.values()
                if t.technique == "multi_hop" and t.split == "dev"}
    assert dev_goal == {0: "unauthorized_action", 1: "exfiltration", 2: "sabotage"}
    # Fixed-goal techniques: r0..r8, channel major.
    for name in ("self_replicating", "approval_manipulation"):
        mine = sorted((t for t in templates.values() if t.technique == name),
                      key=lambda t: t.replica)
        assert [t.replica for t in mine] == list(range(9))
        assert [t.channel for t in mine] == ["tool_output"] * 3 + ["file"] * 3 + [
            "tool_description"] * 3
    assert case_id("banking", "multi_hop", "sabotage", 2) in templates


def test_the_step_8_sets_come_out_of_the_grid(data: Dataset) -> None:
    """PLAN_M3 step 8: set J = the 7 sabotage dev cases, set C = 8 tool_description dev cases plus
    multi_hop's U dev case; they overlap on 2. A check that the grid and the replica convention
    are the plan's."""
    dev = [c for c in new_cases(data) if data.split_of[c.id] == "dev"]
    j = {c.id for c in dev if c.goal == "sabotage"}
    c_set = {c.id for c in dev if c.channel == "tool_description"} | {
        c.id for c in dev if c.technique == "multi_hop" and c.goal == "unauthorized_action"
    }
    assert len(j) == 7 and len(c_set) == 9 and len(j | c_set) == 14 and len(j & c_set) == 2
    assert case_id("banking", "multi_hop", "sabotage", 2) in j
    assert case_id("banking", "multi_hop", "unauthorized_action", 0) in c_set
    assert {c.technique for c in dev if c.id in j} == set(NEW_TECHNIQUES[:7])


def test_the_eligibility_table_matches_the_suite(comp: Composition) -> None:
    suite = load_banking_suite()
    env = suite.load_and_inject_default_environment({})
    writes_payment = {
        task_id for task_id in banking_task_ids()
        if any(call.function in PAYMENT_TOOLS for call in
               suite.get_user_task_by_id(task_id).ground_truth(
                   suite.get_user_task_by_id(task_id).init_environment(env)))
    }
    assert {int(t.rsplit("_", 1)[1]) for t in writes_payment} == {0, 2, 3, 4, 5, 6, 9, 11, 12, 15}
    own = {c.user_task_id: c.vectors[0] for c in BANKING.baseline()}
    by_channel = {
        "tool_output": {u for u, v in own.items() if v == "injection_incoming_transaction"},
        "file": {u for u, v in own.items() if v != "injection_incoming_transaction"},
        "tool_description": set(own),
    }
    by_channel["multi"] = by_channel["tool_output"]
    for channel, eligible in comp.eligibility.items():
        assert set(eligible.ux) == by_channel[channel], channel
        assert set(eligible.sp) == by_channel[channel] & writes_payment, channel


# --- every invariant of the dataset and the split -----------------------------------------------


def test_ids_are_unique_and_every_row_loads(data: Dataset, files: dict[str, str]) -> None:
    ids = [c.id for c in data.cases]
    assert len(set(ids)) == len(ids) == 225
    rows = [json.loads(line) for line in files["banking_v1.jsonl"].splitlines()]
    assert [r["id"] for r in rows] == ids
    assert load_cases(rows) == data.cases  # the derived fields are checked on load
    assert all(re.fullmatch(r"banking:[a-z_]+:[a-z_]+:r\d", i) for i in ids[144:])
    assert all(i.startswith("banking:important_instructions:user_task_") for i in ids[:144])


def test_each_cases_channel_and_stratum_are_derived_from_its_vectors(data: Dataset) -> None:
    for c in data.cases:
        assert c.channel == channel_for(c.vectors), c.id
        assert c.t2_stratum == t2_stratum_for(c.vectors, c.injection_task_id), c.id
        if c.technique == "multi_hop":
            assert c.channel == "multi" and c.t2_stratum == "cross_worker"
    sabotage = {c.check for c in data.cases if c.goal == "sabotage"}
    assert sabotage == {"lab_sabotage_0"}
    assert {c.check for c in data.cases if c.goal == "propagation"} == {"lab_replicate_0"}
    assert {c.injection_task_id for c in data.cases if c.technique == "approval_manipulation"} <= {
        "injection_task_4", "injection_task_5", "injection_task_6"}


def test_a_new_case_sits_on_an_eligible_pair_of_its_goal(data: Dataset, comp: Composition) -> None:
    by_id = {t.id: t for t in generate_templates(comp)}
    for c in new_cases(data):
        assert pair(c) in by_id[c.id].eligible_pairs, c.id


def test_the_d84_pair_is_dev_and_in_known_issues(
    data: Dataset, files: dict[str, str]
) -> None:
    assert data.placed.pair_split[D84_PAIR] == "dev"
    for c in data.cases:
        if pair(c) == D84_PAIR:
            assert data.split_of[c.id] == "dev"
    (entry,) = json.loads(files["banking_known_issues.json"])
    assert (entry["user_task_id"], entry["injection_task_id"]) == D84_PAIR
    assert entry["split"] == "dev" and "D84" in entry["note"]
    assert KNOWN_PAIR_ISSUES[D84_PAIR] == entry["note"]


def test_no_pair_is_in_both_splits_and_every_case_has_its_pairs_split(data: Dataset) -> None:
    splits_of_pair: dict[tuple[str, str], set[str]] = {}
    for c in data.cases:
        splits_of_pair.setdefault(pair(c), set()).add(data.split_of[c.id])
        assert data.split_of[c.id] == data.placed.pair_split[pair(c)]
    assert all(len(s) == 1 for s in splits_of_pair.values())
    assert set(splits_of_pair) <= set(data.placed.pair_split)
    # The pairs file is exactly the pairs the dataset uses.
    assert set(data.placed.pair_split) == set(splits_of_pair)


def test_the_dev_share_of_the_pairs_is_round_30_percent(data: Dataset, comp: Composition) -> None:
    n_pairs = len(data.placed.pair_split)
    n_dev = sum(1 for s in data.placed.pair_split.values() if s == "dev")
    assert n_dev == round(comp.dev_fraction * n_pairs)
    assert n_pairs == 144 + len({pair(c) for c in new_cases(data)} - {pair(c) for c in
                                                                     data.cases[:144]})


def test_the_split_file_agrees_with_the_build(files: dict[str, str], data: Dataset) -> None:
    split = json.loads(files["banking_split.json"])
    assert split["cases"] == data.split_of
    assert split["n_pairs"] == len(split["pairs"])
    assert split["pair_seed"] == "20261028:banking:pairs"
    assert Counter(split["cases"].values()) == {"dev": 68, "test": 157}


def test_dev_and_test_wordings_share_no_run_of_four_words() -> None:
    """The wordings used by the new cases: a defense tuned on dev phrasing is not tuned on test."""
    def runs(texts: list[str]) -> set[tuple[str, ...]]:
        out: set[tuple[str, ...]] = set()
        for text in texts:
            words = re.sub(r"\{[a-z]+\}", " ", text).lower().split()
            out |= {tuple(words[i : i + 4]) for i in range(len(words) - 3)}
        return out

    used = set(NEW_TECHNIQUES)
    dev = [v["dev"] for t, variants in techniques.TEMPLATES.items() if t in used
           for v in variants.values()]
    test = [v["test"] for t, variants in techniques.TEMPLATES.items() if t in used
            for v in variants.values()]
    assert set(dev).isdisjoint(test) and runs(dev).isdisjoint(runs(test))
    assert set(techniques.TEMPLATES) == used


def test_every_new_case_can_be_placed_in_its_split(data: Dataset) -> None:
    """The dataset is usable: place() builds each payload, including every obfuscated cell
    (prerequisite A) and the cases on the lab tasks, whose goal text and markers it supplies."""
    for c in new_cases(data):
        placement = place(c, data.split_of[c.id])
        assert placement.injections or placement.tool_descriptions
        goals = LAB_GOAL_MARKERS if c.injection_task_id in LAB_GOAL_MARKERS else GOAL_MARKERS
        assert len(placement.markers) == len(goals[c.injection_task_id]) + len(c.vectors)


# --- the subsets --------------------------------------------------------------------------------


def subset(files: dict[str, str], name: str) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(files[f"banking_{name}.json"])
    return out


def test_subset_sizes_nesting_and_membership(data: Dataset, files: dict[str, str]) -> None:
    screen = subset(files, "test_screen")["ids"]
    s120 = subset(files, "120b")["ids"]
    repeat = subset(files, "repeat")["ids"]
    assert (len(screen), len(s120), len(repeat)) == (50, 25, 10)
    assert all(len(set(x)) == len(x) for x in (screen, s120, repeat))
    test_ids = {i for i, s in data.split_of.items() if s == "test"}
    assert set(screen) <= test_ids  # test-screen ⊂ test
    assert set(s120) <= set(screen)  # 25 ⊂ 50
    assert set(repeat) <= set(screen)  # 10 ⊂ 50
    assert len(set(repeat) & set(s120)) == 5  # exactly 5 of the 10 are in the 25
    assert set(screen) & {i for i, s in data.split_of.items() if s == "dev"} == set()


def test_the_repeat_subset_is_for_the_topology_sweep_only(files: dict[str, str]) -> None:
    assert "topology sweep only" in subset(files, "repeat")["used_in"]
    assert subset(files, "repeat")["split_by"] == "120b"
    assert subset(files, "test_screen")["split_by"] is None


def test_quotas_and_marginals_match_the_algorithm(data: Dataset, files: dict[str, str]) -> None:
    levels = levels_of(data.cases)
    assert levels == {"channel": 4, "technique": 10, "goal": 4, "user_task": 16}
    for name, n in (("test_screen", 50), ("120b", 25), ("repeat", 10)):
        out = subset(files, name)
        assert out["base_quota"] == {
            "channel": -(-n // 4), "technique": -(-n // 10), "goal": -(-n // 4),
            "user_task": -(-n // 16),
        }
        by_id = {c.id: c for c in data.cases}
        chosen = [by_id[i] for i in out["ids"]]
        # The stored marginals are the cases', and no level exceeds its (raised) quota.
        assert out["marginals"]["goal"] == dict(sorted(Counter(c.goal for c in chosen).items()))
        for attr, counts in out["marginals"].items():
            assert sum(counts.values()) == n
            assert max(counts.values()) <= out["base_quota"][attr] + out["quotas_raised_by"]


def test_a_draw_that_cannot_reach_n_stops_with_the_marginals(data: Dataset) -> None:
    pool = [c for c in data.cases if data.split_of[c.id] == "test"]
    with pytest.raises(SamplingError, match="fewer than n"):
        quota_sample(pool[:5], 10, random.Random("x"), levels_of(data.cases))
    # A group cap that makes n unreachable: at most 3 of each group, but n = 10.
    with pytest.raises(SamplingError, match="Cannot reach n = 10.*Marginals"):
        quota_sample(pool, 10, random.Random("x"), levels_of(data.cases),
                     group_of=lambda c: c.goal == "sabotage", per_group=3)


@pytest.mark.parametrize("module", ["composition", "pairs", "sampling", "dataset"])
def test_the_scripts_never_touch_the_run_store(module: str) -> None:
    """D92: subsets come from design attributes only. None of the scripts imports the store, the
    scoring code or sqlite, so no outcome can reach a draw."""
    source = (Path(__file__).parent.parent / "lab" / "attacks" / f"{module}.py").read_text(
        encoding="utf-8")
    imports = re.findall(r"^(?:from|import) ([\w.]+)", source, flags=re.M)
    assert not [i for i in imports if i.startswith(("lab.store", "lab.scoring", "sqlite3"))]


# --- pair placement: lookahead, tightest strata, infeasible compositions ------------------------


def pc(case_id_: str, split: str, *pairs: tuple[str, str]) -> PairCase:
    return PairCase(case_id_, split, pairs)  # type: ignore[arg-type]


def p(user: int, inj: str = "inj") -> tuple[str, str]:
    return (f"user_task_{user}", inj)


FILLER = {p(i, "base") for i in range(40)}  # baseline pairs, so the dev share is reachable


def test_the_tightest_strata_file_x_s_and_file_x_p_are_feasible() -> None:
    """§3.5: file x S has 3 pairs and 6 cases (2 dev); file x P has 3 pairs and 3 cases (1 dev)."""
    s_pairs = tuple(p(u, "lab_sabotage_0") for u in (0, 2, 12))
    p_pairs = tuple(p(u, "lab_replicate_0") for u in (0, 2, 12))
    cases = [pc(f"s{k}", "dev" if k < 2 else "test", *s_pairs) for k in range(6)]
    cases += [pc(f"p{k}", "dev" if k < 1 else "test", *p_pairs) for k in range(3)]
    for seed in range(20):
        placed = place_pairs(cases, FILLER, forced_dev=[], dev_fraction=0.30,
                             rng=random.Random(f"tight:{seed}"))
        for c in cases:
            assert placed.pair_of[c.id] in c.eligible
            assert placed.pair_split[placed.pair_of[c.id]] == c.split
        dev_pairs = {placed.pair_of[c.id] for c in cases if c.split == "dev"}
        test_pairs = {placed.pair_of[c.id] for c in cases if c.split == "test"}
        assert dev_pairs.isdisjoint(test_pairs)


def test_the_real_composition_places_its_tightest_strata(data: Dataset) -> None:
    file_s = [c for c in new_cases(data) if c.channel == "file" and c.goal == "sabotage"]
    file_p = [c for c in new_cases(data) if c.channel == "file" and c.goal == "propagation"]
    assert (len(file_s), len(file_p)) == (6, 3)
    for stratum, dev in ((file_s, 2), (file_p, 1)):
        assert len({pair(c) for c in stratum}) <= 3
        assert sum(data.split_of[c.id] == "dev" for c in stratum) == dev
        assert {pair(c) for c in stratum if data.split_of[c.id] == "dev"}.isdisjoint(
            {pair(c) for c in stratum if data.split_of[c.id] == "test"})


def test_the_lookahead_keeps_a_later_case_from_taking_anothers_last_pair() -> None:
    """a and b are taken by dev cases, so test case R is left with c. Dev case C (3 pairs, placed
    first, being dev) could take c; locking it would leave R with nothing. Without the lookahead
    this fails for some seeds."""
    a, b, c, d, e = p(1), p(2), p(3), p(4), p(5)
    cases = [pc("x1", "dev", a), pc("x2", "dev", b), pc("C", "dev", c, d, e),
             pc("R", "test", a, b, c)]
    chosen = set()
    for seed in range(60):
        placed = place_pairs(cases, FILLER, forced_dev=[], dev_fraction=0.30,
                             rng=random.Random(f"look:{seed}"))
        assert placed.pair_of["R"] == c and placed.pair_split[c] == "test"
        chosen.add(placed.pair_of["C"])
    assert chosen == {d, e}  # C never takes c, and both others are possible


def test_a_case_prefers_an_unused_pair_and_the_user_task_with_the_fewest_new_cases() -> None:
    first = [pc(f"a{k}", "test", p(1), p(2), p(3)) for k in range(3)]
    placed = place_pairs(first, FILLER, forced_dev=[], dev_fraction=0.30, rng=random.Random("u"))
    assert sorted(placed.pair_of.values()) == [p(1), p(2), p(3)]  # three cases, three pairs
    more = [pc(f"b{k}", "test", p(1), p(2), p(3)) for k in range(4)]
    placed = place_pairs(more, FILLER, forced_dev=[], dev_fraction=0.30, rng=random.Random("u"))
    counts = Counter(placed.pair_of.values())
    assert sorted(counts.values()) == [1, 1, 2]  # spread, never 3 on one while another is empty


def test_fewest_eligible_pairs_go_first_dev_before_test_then_by_id() -> None:
    """The order is observable: the forced pair is only free for the first dev case that can
    take it, and a case with a single eligible pair is always placed before wider ones."""
    wide = pc("a_wide", "dev", p(1), p(2))
    narrow = pc("z_narrow", "dev", p(1))
    placed = place_pairs([wide, narrow], FILLER, forced_dev=[], dev_fraction=0.30,
                         rng=random.Random("o"))
    assert placed.pair_of["z_narrow"] == p(1) and placed.pair_of["a_wide"] == p(2)


def test_the_forced_dev_pair_is_locked_before_placement() -> None:
    forced = p(7)
    test_case = pc("t", "test", forced)
    with pytest.raises(PlacementError, match="no feasible pair"):
        place_pairs([test_case], FILLER, forced_dev=[forced], dev_fraction=0.30,
                    rng=random.Random("f"))
    ok = place_pairs([pc("d", "dev", forced)], FILLER, forced_dev=[forced], dev_fraction=0.30,
                     rng=random.Random("f"))
    assert ok.pair_of["d"] == forced and ok.pair_split[forced] == "dev"


def test_an_infeasible_stratum_stops_with_an_error_naming_the_case() -> None:
    """One eligible pair for a dev case and a test case: a pair carries a single split. The dev
    case is placed first (dev before test) and cannot lock the pair, since the test case would
    be left with none, so it is the one named."""
    cases = [pc("dev_case", "dev", p(1)), pc("test_case", "test", p(1))]
    with pytest.raises(PlacementError, match="dev_case .*no feasible pair"):
        place_pairs(cases, FILLER, forced_dev=[], dev_fraction=0.30, rng=random.Random("i"))


def test_an_unreachable_dev_share_stops_with_an_error() -> None:
    cases = [pc(f"d{k}", "dev", p(k, "x")) for k in range(30)]  # 30 dev pairs, 21 allowed
    with pytest.raises(PlacementError, match="cannot reach the share"):
        place_pairs(cases, FILLER | {p(k, "x") for k in range(30)}, forced_dev=[],
                    dev_fraction=0.30, rng=random.Random("s"))


def test_the_script_stops_cleanly_on_an_infeasible_composition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Remove all but one file user task from the sabotage eligibility: file x S has dev and test
    cases but one pair. build() raises PlacementError; main() prints it and returns 1."""
    raw = yaml.safe_load((DATA_DIR / "banking_composition.yaml").read_text(encoding="utf-8"))
    raw["eligibility"]["file"]["sp"] = ["user_task_0"]
    (tmp_path / "banking_composition.yaml").write_text(
        yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    broken = load_composition(tmp_path / "banking_composition.yaml")
    with pytest.raises(PlacementError, match="no feasible pair"):
        build_dataset(BANKING, broken)
    assert main(["banking", "--data-dir", str(tmp_path)]) == 1
    assert "Stopped, nothing relaxed" in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir()] == ["banking_composition.yaml"]  # wrote nothing


def test_a_copy_of_the_real_composition_writes_the_same_bytes(tmp_path: Path) -> None:
    """Writing into another directory gives files equal to the committed ones, so the check does
    not depend on where the files are."""
    shutil.copy(DATA_DIR / "banking_composition.yaml", tmp_path)
    assert main(["banking", "--data-dir", str(tmp_path)]) == 0
    for name in ("banking_v1.jsonl", "banking_split.json", "banking_test_screen.json",
                 "banking_120b.json", "banking_repeat.json", "banking_known_issues.json"):
        assert (tmp_path / name).read_bytes() == (DATA_DIR / name).read_bytes()
