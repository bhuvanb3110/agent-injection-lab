"""The committed attack dataset, read back for the runner (PLAN_M3 step 8, D112).

`lab.attacks.dataset` writes `data/attack_cases/<domain>_v1.jsonl` and `<domain>_split.json`; this
module reads them. It never regenerates anything: the regeneration check is `python -m
lab.attacks.dataset <domain> --check` (and `lab validate-rules`, step 10).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from lab.attacks.cases import AttackCase, load_cases
from lab.attacks.composition import Split

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "attack_cases"
CASES_VERSION = "v1"  # the same file name part as lab.attacks.dataset.CASES_VERSION


@dataclass(frozen=True)
class CaseSet:
    cases: dict[str, AttackCase]  # case ID -> case, in file order
    split_of: dict[str, Split]  # case ID -> "dev" or "test"


def read_case_set(domain: str, directory: Path = DATA_DIR) -> CaseSet:
    rows = [
        json.loads(line)
        for line in (directory / f"{domain}_{CASES_VERSION}.jsonl")
        .read_text(encoding="utf-8").splitlines()
        if line
    ]
    cases = {c.id: c for c in load_cases(rows)}  # derived fields are re-checked here
    split_of = json.loads(
        (directory / f"{domain}_split.json").read_text(encoding="utf-8")
    )["cases"]
    if set(split_of) != set(cases):
        raise ValueError(f"{domain}: the split file and the case file name different cases")
    return CaseSet(cases, split_of)


@cache
def committed_case_set(domain: str) -> CaseSet:
    """The set in `data/attack_cases/`, read once per process."""
    return read_case_set(domain)
