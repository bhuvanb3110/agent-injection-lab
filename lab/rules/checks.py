"""`lab validate-rules`, group A: the checks that need no run store and run in CI (PLAN_M3 §5,
D102, D117).

Every check takes the repository root and returns a `Check`: its name, and one message per
problem. A check never raises for a problem with the repository's files: a missing, tampered or
malformed file is the thing it exists to report. Group A passes when every check is ok, and an
absent PREREGISTRATION.md fails the checks that read it, so group A cannot pass before step 11.

| Check | § | Fails when |
|---|---|---|
| A1 pinned hashes | 1 | a pinned file is missing or its raw bytes differ; no readable pin block |
| A2 unpinned files | 2 | a file under data/attack_cases/ (or the golden file) is not pinned |
| A2 freeze log | 2 | a freeze is invalid, or an unblinding / judge record is |
| A2 defense configs | 2 | an experiment with test cases names a defense config not frozen |
| A3 schema | 3 | a row does not load, an ID repeats, a predicate is not registered |
| A4 split | 4 | a case is split twice or not at all, a pair is in both splits, the D84 pair |
|  |  | is not dev, or a dev and a test wording share four words in a row |
| A5 subset nesting | 5 | test-screen is not inside test, or 120b / repeat not inside test-screen |
| A6 regeneration | 6 | the scripts do not reproduce the committed files byte for byte |
| A7 golden fixtures | 7 | a rule answers a fixture differently |
| A8 sampling | 8 | providers.yaml and the prereg disagree on temperature or reasoning effort |
| A9 t2_stratum | 9 | a stored channel, stratum or hop path differs from what WORKER_TOOLS gives |
| A10 deterministic only | 10 | the combination is no longer T4 (D108) |
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lab.rules.golden import GOLDEN_PATH, golden_problems
from lab.rules.prereg import (
    DEFENSE_CONFIG_DIR,
    JUDGE_MIN_KAPPA,
    PreregError,
    freeze_problems,
    load_freeze_log,
    load_pins,
    load_values,
    sha256_file,
    unblinding_problems,
)
from lab.rules.scope import DATA_SUBDIR, DOMAIN, load_split_data, scope_of_raw

# D91: the pair whose outcomes were seen in M2 and whose check has a blind spot (D84).
D84_PAIR = ("user_task_2", "injection_task_4")
# The nesting of the subsets (D92): each must be inside the one it is drawn from.
SUBSET_PARENTS = {"test_screen": "test", "120b": "test_screen", "repeat": "test_screen"}
# Files group A needs pinned besides everything under data/attack_cases/.
REQUIRED_PINS = (GOLDEN_PATH,)


@dataclass(frozen=True)
class Check:
    name: str
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def _data_dir(root: Path) -> Path:
    return root / DATA_SUBDIR


def _read_json(path: Path) -> Any:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [k for k, _ in pairs]
        repeated = sorted({k for k in keys if keys.count(k) > 1})
        if repeated:
            raise ValueError(f"{path.name}: repeated key(s): {repeated[:3]}")
        return dict(pairs)

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)


def _rows(root: Path) -> list[dict[str, Any]]:
    path = _data_dir(root) / f"{DOMAIN}_v1.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- A1, A2: the pin block ------------------------------------------------------------------


def check_pins(root: Path) -> Check:
    name = "A1 pinned hashes"
    try:
        pins = load_pins(root)
    except PreregError as error:
        return Check(name, (str(error),))
    problems = []
    for path, digest in sorted(pins.items()):
        file = root / path
        if not file.is_file():
            problems.append(f"{path}: pinned, but the file does not exist")
        elif sha256_file(file) != digest:
            problems.append(f"{path}: raw bytes differ from the pin")
    return Check(name, tuple(problems))


def check_unpinned(root: Path) -> Check:
    name = "A2 unpinned files"
    try:
        pins = load_pins(root)
    except PreregError as error:
        return Check(name, (f"no pin block to compare with ({error})",))
    data = _data_dir(root)
    found = sorted(p.relative_to(root).as_posix() for p in data.rglob("*") if p.is_file())
    problems = [f"{p}: under {DATA_SUBDIR}/ but not pinned" for p in found if p not in pins]
    problems += [f"{p}: must be pinned (group A reads it) but is not" for p in REQUIRED_PINS
                 if p not in pins]
    return Check(name, tuple(problems))


def check_freeze_log(root: Path) -> Check:
    """The freeze log, if there is one: every freeze valid, the unblinding record valid, and the
    judge registered with kappa at least 0.6. An absent log is fine: nothing is frozen yet."""
    name = "A2 freeze log"
    try:
        log = load_freeze_log(root)
    except PreregError as error:
        return Check(name, (str(error),))
    if log is None:
        return Check(name)
    _, problems = freeze_problems(log, root)
    if log.unblinding is not None:
        problems += [f"unblinding record: {p}" for p in unblinding_problems(log, root)]
    if log.judge is not None and log.judge.kappa < JUDGE_MIN_KAPPA:
        problems.append(f"judge registration: kappa {log.judge.kappa} is below {JUDGE_MIN_KAPPA}")
    return Check(name, tuple(problems))


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, Mapping):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


def check_defense_configs(root: Path) -> Check:
    """A defense config an experiment with test-split cases names must be frozen, with the hash
    the file has now, before that experiment's first run (PLAN_M3 §5.2, D102).

    The experiment says which defense config it uses by naming its path under configs/defenses/
    anywhere in its YAML, whatever the field is called, so this works for the field M4 adds. A
    config under configs/defenses/ that no such experiment names is a draft and needs no freeze.
    The time order (frozen before the first test-screen *run*) is the store's, in group B."""
    name = "A2 defense configs"
    experiments = root / "configs" / "experiments"
    if not experiments.is_dir():
        return Check(name)
    try:
        log = load_freeze_log(root)
        frozen = freeze_problems(log, root)[0] if log is not None else {}
        data = load_split_data(root)
    except (PreregError, OSError, ValueError) as error:
        return Check(name, (f"cannot read the freeze log or the split: {error}",))
    frozen_paths = {f.config for f in frozen.values()}
    problems = []
    for path in sorted(experiments.glob("*.yaml")):
        rel = path.relative_to(root).as_posix()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            used = sorted({s for s in _strings(raw) if s.startswith(f"{DEFENSE_CONFIG_DIR}/")})
            if not used or not scope_of_raw(raw, data).has_test:
                continue  # no defense named, or a defense dev run on dev cases: no freeze
        except (yaml.YAMLError, ValueError, KeyError, TypeError) as error:
            problems.append(f"{rel}: cannot tell its cases ({error})")
            continue
        problems += [f"{rel}: runs test-split cases with {d}, which is not frozen in the "
                     f"freeze log" for d in used if d not in frozen_paths]
    return Check(name, tuple(problems))


# --- A3, A9: the dataset ---------------------------------------------------------------------


def check_schema(root: Path) -> Check:
    from pydantic import ValidationError

    from lab.attacks.cases import load_cases

    name = "A3 schema, unique IDs, registered predicates"
    try:
        rows = _rows(root)
    except (OSError, ValueError) as error:
        return Check(name, (f"{DOMAIN}_v1.jsonl cannot be read: {error}",))
    ids = [r.get("id") for r in rows]
    problems = [f"case ID {i!r} appears {ids.count(i)} times" for i in sorted(
        {i for i in ids if ids.count(i) > 1}, key=str)]
    try:
        load_cases(rows)  # the schema, the derived fields, the registered predicates
    except (ValidationError, ValueError) as error:
        problems.append(f"a row does not load: {str(error).splitlines()[0]}")
    return Check(name, tuple(problems))


def check_t2_strata(root: Path) -> Check:
    """Channel, stratum and hop path of every stored row against what the vectors, the injection
    task's write tools and T2's WORKER_TOOLS give (PLAN_M3 §3.1, D94)."""
    from lab.attacks.cases import channel_for, required_hops_for, t2_stratum_for

    name = "A9 t2_stratum matches WORKER_TOOLS"
    try:
        rows = _rows(root)
    except (OSError, ValueError) as error:
        return Check(name, (f"{DOMAIN}_v1.jsonl cannot be read: {error}",))
    problems = []
    for row in rows:
        try:
            computed = {
                "channel": channel_for(row["vectors"]),
                "t2_stratum": t2_stratum_for(row["vectors"], row["injection_task_id"]),
                "required_hops": required_hops_for(row["vectors"], row["injection_task_id"]),
            }
        except (KeyError, ValueError) as error:
            problems.append(f"{row.get('id')}: cannot be computed ({error})")
            continue
        problems += [f"{row['id']}: {k} is {row.get(k)!r}, WORKER_TOOLS gives {v!r}"
                     for k, v in computed.items() if row.get(k) != v]
    return Check(name, tuple(problems))


# --- A4, A5: the split and the subsets ---------------------------------------------------------


def word_fours(template: str) -> set[tuple[str, ...]]:
    """Runs of four words, ignoring any run that touches a placeholder."""
    words = re.sub(r"\{\w+\}", " <ph> ", template).casefold().split()
    return {
        run
        for i in range(len(words) - 3)
        if "<ph>" not in (run := tuple(re.sub(r"\W", "", w) for w in words[i : i + 4]))
    }


def wording_overlap() -> list[tuple[str, ...]]:
    """Runs of four words that a dev wording and a test wording share (PLAN_M3 §5.4, D95)."""
    from lab.attacks.techniques import TEMPLATES

    dev: set[tuple[str, ...]] = set()
    test: set[tuple[str, ...]] = set()
    for variants in TEMPLATES.values():
        for by_split in variants.values():
            dev |= word_fours(by_split["dev"])
            test |= word_fours(by_split["test"])
    return sorted(dev & test)


def check_split(root: Path) -> Check:
    name = "A4 split invariants"
    data = _data_dir(root)
    try:
        rows = _rows(root)
        split = _read_json(data / f"{DOMAIN}_split.json")
        known = _read_json(data / f"{DOMAIN}_known_issues.json")
        split_of: dict[str, str] = split["cases"]
        pair_split: dict[str, str] = split["pairs"]
    except (OSError, ValueError, KeyError) as error:
        return Check(name, (f"the dataset or split cannot be read: {error}",))
    problems = []
    ids = [r["id"] for r in rows]
    # Each case once: a repeated key is refused when the file is read, so compare the sets.
    problems += [f"{i}: a case with no split" for i in sorted(set(ids) - set(split_of))]
    problems += [f"{i}: in the split but not a case" for i in sorted(set(split_of) - set(ids))]
    problems += [f"{i}: split is {s!r}, not dev or test" for i, s in sorted(split_of.items())
                 if s not in ("dev", "test")]
    for row in rows:
        pair = f"{row['user_task_id']}|{row['injection_task_id']}"
        if pair not in pair_split:
            problems.append(f"{row['id']}: its pair {pair} has no split")
        elif row["id"] in split_of and pair_split[pair] != split_of[row["id"]]:
            problems.append(f"{row['id']} is {split_of[row['id']]}, but its pair {pair} is "
                            f"{pair_split[pair]}: a pair would be in both splits")
    forced = [tuple(p) for p in split.get("forced_dev_pairs", [])]
    for pair_tuple in {D84_PAIR, *forced}:
        key = "|".join(pair_tuple)
        if pair_split.get(key) != "dev":
            problems.append(f"{key} must be dev (D91) but is {pair_split.get(key)!r}")
    if D84_PAIR not in {(k["user_task_id"], k["injection_task_id"]) for k in known}:
        problems.append("the D84 pair is not in the known issues")
    for overlap in wording_overlap():
        problems.append(f"a dev and a test wording share: {' '.join(overlap)!r}")
    return Check(name, tuple(problems))


def check_nesting(root: Path) -> Check:
    name = "A5 subset nesting"
    data = _data_dir(root)
    try:
        split_of: dict[str, str] = _read_json(data / f"{DOMAIN}_split.json")["cases"]
        subsets = {n: _read_json(data / f"{DOMAIN}_{n}.json") for n in SUBSET_PARENTS}
    except (OSError, ValueError, KeyError) as error:
        return Check(name, (f"a subset or the split cannot be read: {error}",))
    members = {n: set(s["ids"]) for n, s in subsets.items()}
    members["test"] = {i for i, s in split_of.items() if s == "test"}
    problems = []
    for subset, parent in SUBSET_PARENTS.items():
        ids = subsets[subset]["ids"]
        if len(ids) != len(members[subset]):
            problems.append(f"{subset}: a case is listed twice")
        if len(ids) != subsets[subset]["n"]:
            problems.append(f"{subset}: lists {len(ids)} cases, its n is {subsets[subset]['n']}")
        outside = sorted(members[subset] - members[parent])
        if outside:
            problems.append(f"{subset} is not inside {parent}: {len(outside)} case(s), first "
                            f"{outside[0]}")
    for subset, body in subsets.items():
        by = body.get("split_by")
        if by is not None and by in members:
            inside = len(members[subset] & members[by])
            if inside != body.get("per_group"):
                problems.append(f"{subset}: {inside} cases inside {by}, expected "
                                f"{body.get('per_group')}")
    return Check(name, tuple(problems))


def regenerated_files(root: Path) -> dict[str, str]:
    """What the generator writes for the composition file under `root` (slow: about 20 s)."""
    from lab.attacks.dataset import BANKING, build

    return build(BANKING, _data_dir(root))[2]


def check_regeneration(root: Path, files: Mapping[str, str] | None = None) -> Check:
    """The scripts reproduce every committed file byte for byte (PLAN_M3 §5.6). `files` is the
    result of `regenerated_files`, when a caller already has it."""
    from lab.attacks.dataset import check_files
    from lab.attacks.pairs import PlacementError
    from lab.attacks.sampling import SamplingError

    name = "A6 regeneration, byte for byte"
    try:
        files = files if files is not None else regenerated_files(root)
    except (PlacementError, SamplingError, OSError, ValueError, KeyError) as error:
        return Check(name, (f"the generator cannot run: {error}",))
    return Check(name, tuple(check_files(files, _data_dir(root))))


# --- A7, A8, A10: rules and settings -------------------------------------------------------------


def check_golden(root: Path) -> Check:
    return Check("A7 golden behaviour fixtures", tuple(golden_problems(root)))


def check_sampling(root: Path) -> Check:
    """providers.yaml's temperature and reasoning effort against the prereg's values block, for
    the same set of models (PLAN_M3 §5.8). Limits and prices are not compared: they are
    re-checked before every run."""
    name = "A8 sampling fields match the prereg"
    try:
        values = load_values(root).sampling
        raw = yaml.safe_load((root / "configs" / "providers.yaml").read_text(encoding="utf-8"))
    except (PreregError, OSError, yaml.YAMLError) as error:
        return Check(name, (str(error),))
    actual = {
        f"{provider}/{model_id}": body["sampling"]
        for provider, p in raw["providers"].items()
        for model_id, body in p["models"].items()
        if body.get("sampling")
    }
    problems = [f"{m}: has sampling in providers.yaml, none in the prereg"
                for m in sorted(set(actual) - set(values))]
    problems += [f"{m}: in the prereg, but providers.yaml has no sampling for it"
                 for m in sorted(set(values) - set(actual))]
    for model in sorted(set(actual) & set(values)):
        want = values[model]
        got = actual[model]
        for field in ("temperature", "reasoning_effort"):
            if got.get(field) != getattr(want, field):
                problems.append(f"{model}: {field} is {got.get(field)!r} in providers.yaml, "
                                f"{getattr(want, field)!r} in the prereg")
    return Check(name, tuple(problems))


def check_deterministic_only(root: Path | None = None) -> Check:
    """D108: the defense sweep's "deterministic only" is T4. It is `policy_v1` at the action
    checkpoint on T2's base, with no amount cap, so it reuses T4's runs."""
    from lab.defenses.policy import Policy, PolicyGate, load_policy
    from lab.models.fake import FakeModel
    from lab.models.llm_element import LabLLM
    from lab.topologies.presets import POLICY_V1, PRESETS, Preset, build_gates, parse_components

    name = "A10 deterministic only == T4"
    mine = Preset("planner_workers", (f"{POLICY_V1}@action",))
    t4 = PRESETS["t4"]
    problems = []
    if (mine.base, mine.components) != (t4.base, t4.components):
        problems.append(f"T4 is {(t4.base, t4.components)}, deterministic only is "
                        f"{(mine.base, mine.components)}")
    if parse_components(mine.components) != parse_components(t4.components):
        problems.append("the parsed components differ")
    if t4.parent != "t2":
        problems.append(f"T4's parent is {t4.parent!r}, not t2")
    llm = LabLLM(FakeModel([]), provider="fake", model_id="scripted")
    gates = build_gates(t4.components, llm)
    if len(gates) != 1 or not isinstance(gates[0], PolicyGate) or gates[0].policy != load_policy():
        problems.append("T4's gates are not the single policy_v1 gate")
    if set(Policy.model_fields) != {"name", "suite", "recipients", "password", "user_info"}:
        problems.append("policy_v1 has fields beyond the recipient and keyword rules (an amount "
                        "cap?); it would no longer be the T4 of D108")
    return Check(name, tuple(problems))


# --- all of group A ----------------------------------------------------------------------------


def run_group_a(root: Path, regenerated: Mapping[str, str] | None = None) -> list[Check]:
    """Every group A check, in the order of PLAN_M3 §5. Run them all even after a failure, so
    one report lists everything that is wrong. A check that raises on a file it did not expect
    is a failed check, never a crash: a malformed file is what group A is there to find."""
    steps: list[tuple[str, Callable[[], Check]]] = [
        ("A1 pinned hashes", lambda: check_pins(root)),
        ("A2 unpinned files", lambda: check_unpinned(root)),
        ("A2 freeze log", lambda: check_freeze_log(root)),
        ("A2 defense configs", lambda: check_defense_configs(root)),
        ("A3 schema, unique IDs, registered predicates", lambda: check_schema(root)),
        ("A4 split invariants", lambda: check_split(root)),
        ("A5 subset nesting", lambda: check_nesting(root)),
        ("A6 regeneration, byte for byte", lambda: check_regeneration(root, regenerated)),
        ("A7 golden behaviour fixtures", lambda: check_golden(root)),
        ("A8 sampling fields match the prereg", lambda: check_sampling(root)),
        ("A9 t2_stratum matches WORKER_TOOLS", lambda: check_t2_strata(root)),
        ("A10 deterministic only == T4", lambda: check_deterministic_only(root)),
    ]
    checks = []
    for name, step in steps:
        try:
            checks.append(step())
        except Exception as error:
            checks.append(Check(name, (f"raised {type(error).__name__}: {error}",)))
    return checks


def format_checks(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        lines.append(f"{'ok    ' if check.ok else 'FAILED'}  {check.name}")
        lines += [f"        {p}" for p in check.problems]
    failed = sum(not c.ok for c in checks)
    lines.append(f"{len(checks) - failed} of {len(checks)} checks ok" if failed == 0
                 else f"{failed} of {len(checks)} checks FAILED")
    return "\n".join(lines)
