"""Builds the attack dataset of a domain from its composition file (PLAN_M3 step 6, D90, D92, D107).

    uv run python -m lab.attacks.dataset banking           # write the files
    uv run python -m lab.attacks.dataset banking --check   # compare them with what is committed

Input: `data/attack_cases/<domain>_composition.yaml` (lab.attacks.composition). Output, in the same
directory:
- `<domain>_v1.jsonl`: the baseline (AgentDojo) cases, then the new cases, one JSON row each;
- `<domain>_split.json`: the split of every pair and of every case (dev / test);
- `<domain>_test_screen.json`, `<domain>_120b.json`, `<domain>_repeat.json`: the subsets, with the
  seed, the quotas and the marginals they were drawn with;
- `<domain>_known_issues.json`: the pairs with a known scoring issue (D91), all dev.

Everything is a pure function of the composition file and the installed task suite. Randomness is
`random.Random(<seed string>)` only, with the seed strings in the composition file, and the
files are written as UTF-8 with LF line endings whatever the platform, so a rerun reproduces them
byte for byte. `--check` is the regeneration check that `lab validate-rules` will run (D102).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab.attacks.cases import (
    TOOL_DESCRIPTION_PREFIX,
    VECTOR_READ_TOOL,
    AttackCase,
    agentdojo_cases,
    agentdojo_predicate,
    channel_for,
    load_cases,
)
from lab.attacks.composition import (
    Composition,
    Pair,
    Split,
    Template,
    generate_templates,
    load_composition,
)
from lab.attacks.pairs import PairCase, Placed, PlacementError, place_pairs
from lab.attacks.predicates import LAB_PREDICATES
from lab.attacks.sampling import Draw, SamplingError, format_marginals, levels_of, quota_sample
from lab.domains.agentdojo_bridge import KNOWN_PAIR_ISSUES

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "attack_cases"
CASES_VERSION = "v1"


@dataclass(frozen=True)
class Domain:
    """What the generator needs from a domain besides its composition file."""

    name: str
    baseline: Callable[[], list[AttackCase]]  # the cases the suite already has (AgentDojo's 144)
    description_vector: Callable[[str], str]  # a vector -> the tool_description vector for it
    check_for: Callable[[str], str]  # injection task -> the name of its success predicate
    known_pair_issues: Mapping[Pair, str]
    load: Callable[[list[dict[str, Any]]], list[AttackCase]]


def _banking_description_vector(own_vector: str) -> str:
    """D94: the description of the read tool that returns the task's own vector."""
    return TOOL_DESCRIPTION_PREFIX + VECTOR_READ_TOOL[own_vector]


def _banking_check(injection_task_id: str) -> str:
    return injection_task_id if injection_task_id in LAB_PREDICATES else agentdojo_predicate(
        injection_task_id
    )


BANKING = Domain(
    name="banking",
    baseline=agentdojo_cases,
    description_vector=_banking_description_vector,
    check_for=_banking_check,
    known_pair_issues=KNOWN_PAIR_ISSUES,
    load=load_cases,
)


@dataclass(frozen=True)
class Dataset:
    cases: list[AttackCase]  # baseline first, then the new cases in grid order
    split_of: dict[str, Split]  # case ID -> split
    placed: Placed
    n_baseline: int
    draws: dict[str, Draw]  # subset name -> its draw, in the order drawn


def build_dataset(domain: Domain, comp: Composition) -> Dataset:
    baseline = domain.baseline()
    own_vector = {c.user_task_id: c.vectors[0] for c in baseline}
    templates = generate_templates(comp)

    placed = place_pairs(
        [PairCase(t.id, t.split, t.eligible_pairs) for t in templates],
        {(c.user_task_id, c.injection_task_id) for c in baseline},
        forced_dev=comp.forced_dev_pairs,
        dev_fraction=comp.dev_fraction,
        rng=random.Random(comp.pair_seed),
    )
    new = domain.load([_row(domain, t, placed.pair_of[t.id], own_vector) for t in templates])
    cases = [*baseline, *new]
    split_of = {c.id: placed.pair_split[(c.user_task_id, c.injection_task_id)] for c in cases}

    for t in templates:  # the grid's split and the pair's must agree
        if split_of[t.id] != t.split:
            raise ValueError(f"{t.id}: grid says {t.split}, its pair is {split_of[t.id]}")
    return Dataset(cases, split_of, placed, len(baseline), _draw_subsets(comp, cases, split_of))


def _row(
    domain: Domain, t: Template, pair: Pair, own_vector: Mapping[str, str]
) -> dict[str, Any]:
    user_task, injection_task = pair
    if t.vectors is not None:
        vectors = list(t.vectors)
    elif t.channel == "tool_description":
        vectors = [domain.description_vector(own_vector[user_task])]
    else:
        vectors = [own_vector[user_task]]
    if channel_for(vectors) != t.channel:
        raise ValueError(f"{t.id}: vectors {vectors} are not in the channel {t.channel}")
    return {
        "id": t.id,
        "suite": domain.name,
        "user_task_id": user_task,
        "injection_task_id": injection_task,
        "technique": t.technique,
        "goal": t.goal,
        "check": domain.check_for(injection_task),
        "vectors": vectors,
    }


def _member_of(ids: Sequence[str]) -> Callable[[AttackCase], bool]:
    inside = set(ids)
    return lambda case: case.id in inside


def _draw_subsets(
    comp: Composition, cases: Sequence[AttackCase], split_of: Mapping[str, Split]
) -> dict[str, Draw]:
    levels = levels_of(cases)
    by_id = {c.id: c for c in cases}
    draws: dict[str, Draw] = {}
    for name, spec in comp.subsets.items():
        if spec.pool == "test":
            pool = [c for c in cases if split_of[c.id] == "test"]
        else:
            pool = [by_id[i] for i in draws[spec.pool].ids]  # an earlier subset (KeyError: order)
        group_of = _member_of(draws[spec.split_by].ids) if spec.split_by is not None else None
        draws[name] = quota_sample(
            pool, spec.n, random.Random(spec.seed), levels,
            group_of=group_of, per_group=spec.per_group,
        )
    return draws


# --- files ---------------------------------------------------------------------------------


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def render_files(domain: Domain, comp: Composition, data: Dataset) -> dict[str, str]:
    """File name -> text, for every file the build writes. LF line endings only."""
    d = comp.domain
    pairs = {f"{u}|{i}": s for (u, i), s in sorted(data.placed.pair_split.items())}
    files = {
        f"{d}_{CASES_VERSION}.jsonl": "".join(
            json.dumps(c.model_dump(), sort_keys=True) + "\n" for c in data.cases
        ),
        f"{d}_split.json": _json({
            "domain": d,
            "pair_seed": comp.pair_seed,
            "dev_fraction": comp.dev_fraction,
            "forced_dev_pairs": comp.forced_dev_pairs,
            "n_pairs": len(pairs),
            "n_dev_pairs": sum(1 for s in pairs.values() if s == "dev"),
            "pairs": pairs,
            "cases": dict(sorted(data.split_of.items())),
        }),
        f"{d}_known_issues.json": _json([
            {
                "user_task_id": u,
                "injection_task_id": i,
                "split": data.placed.pair_split[(u, i)],
                "note": note,
            }
            for (u, i), note in sorted(domain.known_pair_issues.items())
        ]),
    }
    for name, draw in data.draws.items():
        spec = comp.subsets[name]
        files[f"{d}_{name}.json"] = _json({
            "name": name,
            "n": spec.n,
            "pool": spec.pool,
            "seed": spec.seed,
            "used_in": spec.used_in,
            "split_by": spec.split_by,
            "per_group": spec.per_group,
            "base_quota": draw.base_quota,
            "quotas_raised_by": draw.raised_by,
            "marginals": draw.marginals,
            "ids": list(draw.ids),
        })
    for name, text in files.items():
        if "\r" in text:
            raise ValueError(f"{name}: generated text holds a carriage return")
    return files


def write_files(files: Mapping[str, str], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        # newline="\n": text mode on Windows would write CRLF, and the regeneration check is
        # byte for byte.
        with open(directory / name, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


def check_files(files: Mapping[str, str], directory: Path) -> list[str]:
    """The files that are missing or differ from what the build gives: the regeneration check."""
    problems = []
    for name, text in files.items():
        path = directory / name
        if not path.exists():
            problems.append(f"{name}: missing")
        elif path.read_bytes() != text.encode("utf-8"):
            problems.append(f"{name}: differs from the regenerated file")
    return problems


def build(
    domain: Domain, directory: Path = DATA_DIR
) -> tuple[Composition, Dataset, dict[str, str]]:
    comp = load_composition(directory / f"{domain.name}_composition.yaml")
    data = build_dataset(domain, comp)
    return comp, data, render_files(domain, comp, data)


DOMAINS = {BANKING.name: BANKING}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.attacks.dataset", description=__doc__)
    parser.add_argument("domain", choices=sorted(DOMAINS))
    parser.add_argument("--check", action="store_true",
                        help="compare with the committed files instead of writing them")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)
    try:
        comp, data, files = build(DOMAINS[args.domain], args.data_dir)
    except (PlacementError, SamplingError) as error:
        print(f"Stopped, nothing relaxed: {error}", file=sys.stderr)
        return 1
    for name, draw in data.draws.items():
        print(format_marginals(name, draw))
    if args.check:
        problems = check_files(files, args.data_dir)
        for problem in problems:
            print(problem, file=sys.stderr)
        print("Regeneration check:", "FAILED" if problems else "ok, byte for byte")
        return 1 if problems else 0
    write_files(files, args.data_dir)
    print(f"Wrote {len(files)} files to {args.data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
