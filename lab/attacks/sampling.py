"""Quota sampling of the screening subsets (PLAN_M3 §4, D92).

Inputs are design attributes only: user task, channel, technique and goal (the injection task is
part of the case but has no quota). Nothing here opens the run store, so no outcome can reach it.

The algorithm, as D92 states it:
1. Shuffle the pool with `random.Random(seed)`. A string seed is hashed with SHA-512, so
   PYTHONHASHSEED has no effect. The pool is sorted by case ID first, so the shuffle starts from
   a fixed order.
2. Walk the list and accept a case unless it would exceed a quota: ceil(n / levels) for
   channel, technique and goal, and ceil(n / user tasks) for user task.
3. If n is not reached, raise every quota by 1 and walk again. The cases accepted so far stay
   (a pass only adds), so a later pass can only fill what the earlier ones left short.
4. The marginals are returned (and printed by the caller).

One addition for the repeat subset (D92: "exactly 5 of the 10 inside the 25"): a *group cap*. The
cases are in two groups (inside the 120b subset or not) and each group holds at most `per_group`
cases. With n = 2 x per_group that means exactly per_group each. A group cap is not a quota: it
is never raised.

If n cannot be reached even with quotas of n, sampling stops with `SamplingError` and the
marginals, and nothing is relaxed.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass

from lab.attacks.cases import AttackCase

ATTRIBUTES = ("channel", "technique", "goal", "user_task")


class SamplingError(Exception):
    pass


def attribute(case: AttackCase, name: str) -> str:
    return case.user_task_id if name == "user_task" else str(getattr(case, name))


def levels_of(cases: Collection[AttackCase]) -> dict[str, int]:
    """How many levels each attribute has in the whole dataset (the quota's denominator)."""
    return {a: len({attribute(c, a) for c in cases}) for a in ATTRIBUTES}


@dataclass(frozen=True)
class Draw:
    ids: tuple[str, ...]  # sorted
    base_quota: dict[str, int]
    raised_by: int  # how many times every quota was raised
    marginals: dict[str, dict[str, int]]


def quota_sample(
    pool: Sequence[AttackCase],
    n: int,
    rng: random.Random,
    levels: Mapping[str, int],
    *,
    group_of: Callable[[AttackCase], bool] | None = None,
    per_group: int | None = None,
) -> Draw:
    if len(pool) < n:
        raise SamplingError(f"The pool has {len(pool)} cases, fewer than n = {n}")
    base = {a: math.ceil(n / levels[a]) for a in ATTRIBUTES}
    order = sorted(pool, key=lambda c: c.id)
    rng.shuffle(order)
    accepted: dict[str, AttackCase] = {}
    counts: dict[str, Counter[str]] = {a: Counter() for a in ATTRIBUTES}
    groups: Counter[bool] = Counter()
    raised_by = 0
    while True:
        quota = {a: q + raised_by for a, q in base.items()}
        for case in order:
            if len(accepted) == n:
                break
            if case.id in accepted:
                continue
            if any(counts[a][attribute(case, a)] >= quota[a] for a in ATTRIBUTES):
                continue
            if group_of is not None and groups[group_of(case)] >= (per_group or n):
                continue
            accepted[case.id] = case
            for a in ATTRIBUTES:
                counts[a][attribute(case, a)] += 1
            if group_of is not None:
                groups[group_of(case)] += 1
        if len(accepted) == n:
            break
        if raised_by >= n:
            raise SamplingError(
                f"Cannot reach n = {n}: {len(accepted)} accepted with every quota at {n}. "
                f"Marginals so far: {_marginals(counts)}"
            )
        raised_by += 1
    return Draw(tuple(sorted(accepted)), base, raised_by, _marginals(counts))


def _marginals(counts: Mapping[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {a: dict(sorted(counts[a].items())) for a in ATTRIBUTES}


def format_marginals(name: str, draw: Draw) -> str:
    lines = [f"{name}: n = {len(draw.ids)}, quotas {draw.base_quota} raised by {draw.raised_by}"]
    for attr, counts in draw.marginals.items():
        lines.append(f"  {attr:<10} " + ", ".join(f"{v} {n}" for v, n in counts.items()))
    return "\n".join(lines)
