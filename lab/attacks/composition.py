"""The composition of the new attack cases (PLAN_M3 §3.5, D107): a data file in, templates out.

The composition file (`data/attack_cases/<domain>_composition.yaml`) holds everything D107 fixes:
the technique x channel/goal grids, the dev rule, the eligibility table, the injection tasks of
each goal, the seeds and the subset sizes. This module only reads it and expands the grids. No
composition choice is made in code, so the same code serves another domain with its own file.

**The grid.** A technique has an n x n grid (n = 3). Its rows and columns are each one axis:
- the row axis is `channel` or `replica`; the column axis is `goal` or `replica`;
- the axis that is not a channel (or goal) leaves it fixed for the whole technique;
- a case is dev iff col == row + k (mod n), with row and col the positions in the lists. That
  gives n dev and n * (n - 1) test cases, one dev case per row and one per column.

**The replica convention (the one place a case ID depends on a choice).** The ID is
`<domain>:<technique>:<goal>:r<replica>`, so (goal, replica) must be unique within a technique:
- when the columns are goals (the six wording techniques and multi_hop), the replica is the
  ROW position. For the six wording techniques the rows are channels, so the replica is the
  channel row order: r0 = tool_output, r1 = file, r2 = tool_description. For multi_hop the rows
  are replicas, so replica r indexes across the goals: with the dev rule the dev case of
  replica r is the goal in column r (r0 = U, r1 = X, r2 = S);
- when the columns are replicas and the goal is fixed (self_replicating, approval_manipulation)
  the goal alone does not tell the nine cases apart, so the replica is the row-major position
  `row * n + col` (r0..r8), channel major: r0..r2 are tool_output, r3..r5 file, r6..r8
  tool_description.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

Split = Literal["dev", "test"]
Pair = tuple[str, str]  # (user task, injection task)


class TechniqueSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    k: int
    row_axis: Literal["channel", "replica"]
    col_axis: Literal["goal", "replica"]
    rows: list[str]
    cols: list[str]
    channel: str | None = None  # fixed, when the rows are not channels
    goal: str | None = None  # fixed, when the columns are not goals
    # Overrides the goal's injection tasks (approval_manipulation: the payment writers only).
    injection_tasks: list[str] | None = None
    # The vectors of every case of the technique, for a channel that spans several (multi).
    vectors: list[str] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> TechniqueSpec:
        if len(self.rows) != len(self.cols):
            raise ValueError(f"{self.name}: the grid must be square")
        if (self.row_axis == "channel") == (self.channel is not None):
            raise ValueError(f"{self.name}: `channel` is given exactly when rows are replicas")
        if (self.col_axis == "goal") == (self.goal is not None):
            raise ValueError(f"{self.name}: `goal` is given exactly when columns are replicas")
        return self


class SubsetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    n: int
    pool: str  # "test", or the name of an earlier subset
    seed: str
    # Draw so that exactly `per_group` of the cases are in the subset `split_by`, and the rest
    # are not (the repeat subset: 5 of its 10 inside the 120b subset).
    split_by: str | None = None
    per_group: int | None = None
    used_in: str = ""


class Eligibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ux: list[str]  # for unauthorized_action and exfiltration
    sp: list[str]  # for sabotage and propagation (payment writers only)


class Composition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    pair_seed: str
    dev_fraction: float
    # The pairs forced to dev before anything else is placed (D91).
    forced_dev_pairs: list[Pair]
    goal_class: dict[str, Literal["ux", "sp"]]
    goal_injection_tasks: dict[str, list[str]]
    eligibility: dict[str, Eligibility]  # by channel
    techniques: list[TechniqueSpec]
    subsets: dict[str, SubsetSpec]

    @model_validator(mode="after")
    def _consistent(self) -> Composition:
        names = [t.name for t in self.techniques]
        if len(set(names)) != len(names):
            raise ValueError("duplicate technique names")
        return self


def load_composition(path: Path) -> Composition:
    with open(path, encoding="utf-8") as f:
        data: Any = yaml.safe_load(f)
    return Composition.model_validate(data)


@dataclass(frozen=True)
class Template:
    """A new case before it is placed on a (user task, injection task) pair."""

    id: str
    technique: str
    channel: str
    goal: str
    row: int
    col: int
    replica: int
    split: Split
    user_tasks: tuple[str, ...]  # eligible
    injection_tasks: tuple[str, ...]  # eligible
    vectors: tuple[str, ...] | None  # fixed by the technique, or None (the user task decides)

    @property
    def eligible_pairs(self) -> tuple[Pair, ...]:
        return tuple((u, i) for u in self.user_tasks for i in self.injection_tasks)


def case_id(domain: str, technique: str, goal: str, replica: int) -> str:
    return f"{domain}:{technique}:{goal}:r{replica}"


def generate_templates(comp: Composition) -> list[Template]:
    """Every technique's grid, expanded: technique order, then row, then column."""
    templates: list[Template] = []
    for spec in comp.techniques:
        n = len(spec.rows)
        for i, row in enumerate(spec.rows):
            for j, col in enumerate(spec.cols):
                channel = row if spec.row_axis == "channel" else spec.channel
                goal = col if spec.col_axis == "goal" else spec.goal
                if channel is None or goal is None:  # the spec validator rules this out
                    raise ValueError(f"{spec.name}: no channel or goal for cell ({i}, {j})")
                replica = i if spec.col_axis == "goal" else i * n + j
                eligibility = comp.eligibility[channel]
                users = eligibility.ux if comp.goal_class[goal] == "ux" else eligibility.sp
                injections = spec.injection_tasks or comp.goal_injection_tasks[goal]
                templates.append(Template(
                    id=case_id(comp.domain, spec.name, goal, replica),
                    technique=spec.name,
                    channel=channel,
                    goal=goal,
                    row=i,
                    col=j,
                    replica=replica,
                    split="dev" if (j - i - spec.k) % n == 0 else "test",
                    user_tasks=tuple(users),
                    injection_tasks=tuple(injections),
                    vectors=tuple(spec.vectors) if spec.vectors else None,
                ))
    ids = [t.id for t in templates]
    if len(set(ids)) != len(ids):
        raise ValueError("The grids give duplicate case IDs")
    return templates
