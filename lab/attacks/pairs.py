"""Placing cases on (user task, injection task) pairs, and splitting the pairs (PLAN_M3 §3.5, D90,
D107).

A pair carries a single split, so dev and test never share a pair (D90). The new cases already
know their split (the grid's dev rule); this decides which pair each one sits on.

Placement, in this order (D107):
1. Cases with the fewest eligible pairs go first. Among those, dev before test, then by case ID.
2. A case prefers an unused pair (no new case on it yet). It picks the user task with the fewest
   new cases so far; ties, and the injection task of that user task, are drawn with the seeded
   random generator from sorted candidates.
3. A case locks a fresh pair (one with no split yet) only if every remaining case keeps an
   eligible pair afterwards. Otherwise it reuses a pair already locked to its split.
4. If a case has no feasible pair, placement stops with `PlacementError`: the composition goes
   back to Bhuvan, nothing is relaxed.

"Unused" and "fresh" differ for a forced pair: the D84 pair is locked to dev from the start, so it
is fresh to no one, but unused until a case sits on it.

The lookahead in step 3 is exact for this rule: a remaining case needs one eligible pair that is
unlocked or locked to its own split, and locking a pair to a split only takes it from the
remaining cases of the other split. A pair can carry any number of cases, so nothing else limits
a case.

Then the pairs no new case chose are split so that dev holds round(dev_fraction x all pairs),
where "all pairs" are the pairs of the dataset: the baseline (AgentDojo) pairs plus any other pair
a new case sits on. The extra dev pairs are drawn from the unlocked baseline pairs.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from lab.attacks.composition import Pair, Split


class PlacementError(Exception):
    """No feasible pair for a case, or the split cannot reach its dev share."""


@dataclass(frozen=True)
class PairCase:
    id: str
    split: Split
    eligible: tuple[Pair, ...]


@dataclass(frozen=True)
class Placed:
    pair_of: dict[str, Pair]  # case ID -> its pair
    pair_split: dict[Pair, Split]  # every pair of the dataset -> its split


def place_pairs(
    cases: Sequence[PairCase],
    baseline_pairs: Collection[Pair],
    *,
    forced_dev: Collection[Pair],
    dev_fraction: float,
    rng: random.Random,
) -> Placed:
    locked: dict[Pair, Split] = dict.fromkeys(forced_dev, "dev")
    used: set[Pair] = set()
    new_per_user: Counter[str] = Counter()
    order = sorted(cases, key=lambda c: (len(c.eligible), c.split != "dev", c.id))
    pair_of: dict[str, Pair] = {}
    for position, case in enumerate(order):
        remaining = order[position + 1 :]
        pool = _unused(case, remaining, locked, used) or _reusable(case, locked)
        if not pool:
            raise PlacementError(
                f"{case.id} ({case.split}) has no feasible pair among its {len(case.eligible)} "
                f"eligible pairs: {_blocked(case, locked)}"
            )
        fewest = min(new_per_user[u] for u, _ in pool)
        user = rng.choice(sorted({u for u, _ in pool if new_per_user[u] == fewest}))
        pair = rng.choice(sorted(p for p in pool if p[0] == user))
        locked.setdefault(pair, case.split)
        used.add(pair)
        new_per_user[user] += 1
        pair_of[case.id] = pair
    return Placed(pair_of, _split_pairs(locked, baseline_pairs, dev_fraction, rng))


def _usable(pair: Pair, split: Split, locked: dict[Pair, Split]) -> bool:
    return locked.get(pair, split) == split


def _unused(
    case: PairCase, remaining: Sequence[PairCase], locked: dict[Pair, Split], used: set[Pair]
) -> list[Pair]:
    """Eligible pairs with no new case on them that the case may take: already locked to its
    split, or fresh and safe to lock (step 3)."""
    return [
        p for p in case.eligible
        if p not in used and _usable(p, case.split, locked)
        and (p in locked or _keeps_everyone_a_pair(p, case.split, remaining, locked))
    ]


def _keeps_everyone_a_pair(
    pair: Pair, split: Split, remaining: Sequence[PairCase], locked: dict[Pair, Split]
) -> bool:
    """After locking `pair` to `split`, does every remaining case still have an eligible pair?
    Only a case of the other split that holds `pair` can lose its last one."""
    return all(
        any(q != pair and _usable(q, c.split, locked) for q in c.eligible)
        for c in remaining
        if c.split != split and pair in c.eligible
    )


def _reusable(case: PairCase, locked: dict[Pair, Split]) -> list[Pair]:
    return [p for p in case.eligible if locked.get(p) == case.split]


def _blocked(case: PairCase, locked: dict[Pair, Split]) -> str:
    other = sum(1 for p in case.eligible if not _usable(p, case.split, locked))
    return f"{other} are locked to the other split, the rest would leave another case without one"


def _split_pairs(
    locked: dict[Pair, Split],
    baseline_pairs: Collection[Pair],
    dev_fraction: float,
    rng: random.Random,
) -> dict[Pair, Split]:
    """Lock the baseline pairs no new case chose, so dev holds round(dev_fraction x all pairs)."""
    everything = set(baseline_pairs) | set(locked)
    target = round(dev_fraction * len(everything))
    dev_now = sum(1 for s in locked.values() if s == "dev")
    free = sorted(set(baseline_pairs) - set(locked))
    extra = target - dev_now
    if not 0 <= extra <= len(free):
        raise PlacementError(
            f"{len(everything)} pairs want {target} dev pairs, {dev_now} are locked to dev and "
            f"{len(free)} are free: cannot reach the share"
        )
    dev_free = set(rng.sample(free, extra))
    return {**locked, **{p: ("dev" if p in dev_free else "test") for p in free}}
