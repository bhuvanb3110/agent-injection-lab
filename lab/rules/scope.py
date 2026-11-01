"""Which attack cases an experiment or a stored run covers, and which split they are in (D102).

The guard, the blinding and the group B checks all ask the same question: is this a test-split
case, and is it one of the 50 test-screen cases? The answer comes from the committed split and
subset files (`data/attack_cases/<domain>_split.json`, `<domain>_test_screen.json`), read as
plain JSON. They are not re-validated here: that is group A's job, and the guard runs group A
before it lets a test case through.

Runs from before milestone 3 have no `case_id`. An attacked run of milestone 2's
`important_instructions` attack is one of the 144 AgentDojo cases, so its (user task, injection
task) pair names its case.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab.config import ExperimentConfig
from lab.domains.agentdojo_bridge import banking_injection_task_ids, banking_task_ids

DOMAIN = "banking"  # milestones 1-4 are Banking only
DATA_SUBDIR = "data/attack_cases"
BASELINE_TECHNIQUE = "important_instructions"
# The files a `cases:` name can refer to, besides a subset: all cases.
ALL_CASES_NAME = "v1"


def baseline_case_id(user_task_id: str, injection_task_id: str) -> str:
    """The ID of the AgentDojo case for a pair (lab.attacks.cases.agentdojo_cases)."""
    return f"{DOMAIN}:{BASELINE_TECHNIQUE}:{user_task_id}:{injection_task_id}"


def case_id_of(
    case_id: str | None, user_task_id: str, injection_task_id: str | None
) -> str | None:
    """The case a run belongs to: its `case_id`, or the AgentDojo case of its pair, or None for a
    benign run."""
    if case_id is not None:
        return case_id
    if injection_task_id is not None:
        return baseline_case_id(user_task_id, injection_task_id)
    return None


@dataclass(frozen=True)
class SplitData:
    """The split of every case and the test-screen subset, from the committed files."""

    directory: Path
    split_of: Mapping[str, str]  # case ID -> "dev" or "test"
    test_screen: frozenset[str]

    def is_test(self, case_id: str | None) -> bool:
        return case_id is not None and self.split_of.get(case_id) == "test"

    def is_test_screen(self, case_id: str | None) -> bool:
        return case_id is not None and case_id in self.test_screen

    def is_test_confirm(self, case_id: str | None) -> bool:
        """A test-split case that is not one of the 50 screening cases (D90)."""
        return self.is_test(case_id) and not self.is_test_screen(case_id)

    def named_set(self, name: str) -> list[str]:
        """The case IDs a `cases: <name>` refers to: all cases (`banking_v1`) or a subset file
        (`test_screen`, `120b`, `repeat`, with or without the `banking_` prefix)."""
        short = name.removeprefix(f"{DOMAIN}_")
        if short == ALL_CASES_NAME:
            return list(self.split_of)
        path = self.directory / f"{DOMAIN}_{short}.json"
        if short in ("split", "known_issues") or not path.is_file():
            raise ValueError(f"cases: {name!r} is not a set of cases ({path.name} not found)")
        ids: list[str] = _read_json(path)["ids"]
        return ids


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_split_data(root: Path) -> SplitData:
    directory = root / DATA_SUBDIR
    split_of = _read_json(directory / f"{DOMAIN}_split.json")["cases"]
    test_screen = frozenset(_read_json(directory / f"{DOMAIN}_test_screen.json")["ids"])
    return SplitData(directory, split_of, test_screen)


@dataclass(frozen=True)
class Scope:
    """The attack cases an experiment runs. Empty for a benign-only experiment."""

    case_ids: tuple[str, ...]
    data: SplitData

    @property
    def has_test(self) -> bool:
        return any(self.data.is_test(c) for c in self.case_ids)

    @property
    def has_test_screen(self) -> bool:
        return any(self.data.is_test_screen(c) for c in self.case_ids)

    @property
    def has_test_confirm(self) -> bool:
        return any(self.data.is_test_confirm(c) for c in self.case_ids)

    @property
    def has_attack_cases(self) -> bool:
        return bool(self.case_ids)


def scope_of_raw(raw: Mapping[str, Any], data: SplitData) -> Scope:
    """The cases of an experiment given as the parsed YAML: a `cases:` list or named set, or the
    pairs of an `attack:` config. It reads only `cases`, `attack`, `user_tasks` and the
    injection tasks, so it also works on a config with fields a later milestone adds.

    An ID or a pair the dataset does not have is an error; the guard turns it into a refusal,
    because it cannot tell which split the run would be in."""
    cases, attack = raw.get("cases"), raw.get("attack")
    ids: list[str]
    if isinstance(cases, list):
        ids = [str(i) for i in cases]
    elif isinstance(cases, str):
        ids = data.named_set(cases)
    elif attack:
        users = raw["user_tasks"]
        injections = attack["injection_tasks"]
        ids = [
            baseline_case_id(user_task, injection_task)
            for user_task in (banking_task_ids() if users == "all" else users)
            for injection_task in (
                banking_injection_task_ids() if injections == "all" else injections
            )
        ]
    else:
        ids = []
    unknown = [i for i in ids if i not in data.split_of]
    if unknown:
        raise ValueError(f"{len(unknown)} case(s) are not in the dataset, so their split is "
                         f"unknown; first: {unknown[0]}")
    return Scope(tuple(ids), data)


def scope_of(config: ExperimentConfig, data: SplitData) -> Scope:
    return scope_of_raw(config.model_dump(mode="json"), data)
