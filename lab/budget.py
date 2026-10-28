"""`lab budget`: what each model has used and has left of its daily limits (no model calls).

It reads the same data the limiter reads at start-up (D22): the requests in the run store
from the last 24 h. So the numbers match what `lab run` prints and what it will enforce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lab.models.providers import ProviderConfig
from lab.scheduler.limits import DAY_S
from lab.store import RunStore

# How much headroom "the next 50,000 tokens" means: the window frees this many tokens of
# daily quota, counted from now.
FREE_STEP_TOKENS = 50_000


@dataclass(frozen=True)
class ModelBudget:
    key: str  # "provider/model_id", as `lab run` prints it
    tokens_used: int
    tokens_limit: int | None  # tpd; None: no daily token limit
    requests_used: int
    requests_limit: int | None  # rpd; None: no daily request limit
    next_free_at: float | None  # when the next FREE_STEP_TOKENS are free; None: never

    @property
    def tokens_left(self) -> int | None:
        return None if self.tokens_limit is None else max(self.tokens_limit - self.tokens_used, 0)

    @property
    def requests_left(self) -> int | None:
        if self.requests_limit is None:
            return None
        return max(self.requests_limit - self.requests_used, 0)


def budgets(
    providers: dict[str, ProviderConfig],
    store: RunStore,
    now: float,
    model: str | None = None,
) -> list[ModelBudget]:
    """One entry per model in the providers config, or only the one `model` names.

    `model` matches the full "provider/model_id" key or the bare model_id.
    """
    rows = []
    for provider in providers.values():
        for model_id, info in provider.models.items():
            key = f"{provider.name}/{model_id}"
            if model is not None and model not in (key, model_id):
                continue
            history = store.request_history(provider.name, model_id, since=now - DAY_S)
            used = sum(tokens for _, tokens in history)
            rows.append(
                ModelBudget(
                    key=key,
                    tokens_used=used,
                    tokens_limit=info.limits.tpd,
                    requests_used=len(history),
                    requests_limit=info.limits.rpd,
                    next_free_at=_next_free_at(history, used, info.limits.tpd, now),
                )
            )
    return rows


def _next_free_at(
    history: list[tuple[float, int]], used: int, limit: int | None, now: float
) -> float | None:
    """When FREE_STEP_TOKENS more tokens are free: now if they already are, else when the
    oldest usage has left the 24 h window far enough. None if the limit is too small for it."""
    if limit is None:
        return None
    short = FREE_STEP_TOKENS - max(limit - used, 0)
    if short <= 0:
        return now
    freed = 0
    for ts, tokens in history:  # oldest first, as request_history returns them
        freed += tokens
        if freed >= short:
            return ts + DAY_S
    return None  # the whole window is smaller than the step; it never frees that much


def format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def format_budget(b: ModelBudget, now: float) -> list[str]:
    if b.tokens_limit is None:
        tokens = f"{b.tokens_used:,} tokens used in the last 24 h (no daily limit)"
        free = "no daily limit, so no next-free time"
    else:
        tokens = (
            f"{b.tokens_used:,} tokens used of {b.tokens_limit:,} in the last 24 h; "
            f"{b.tokens_left:,} left"
        )
        if b.next_free_at is None:
            free = f"next {FREE_STEP_TOKENS:,} tokens: never (the limit is below that)"
        elif b.next_free_at <= now:
            free = f"next {FREE_STEP_TOKENS:,} tokens: free now"
        else:
            free = (
                f"next {FREE_STEP_TOKENS:,} tokens free at {format_time(b.next_free_at)} "
                "(the oldest usage leaves the 24 h window)"
            )
    if b.requests_limit is None:
        requests = f"{b.requests_used:,} requests in the last 24 h (no daily limit)"
    else:
        requests = (
            f"{b.requests_used:,} requests used of {b.requests_limit:,} in the last 24 h; "
            f"{b.requests_left:,} left"
        )
    return [b.key, f"  tokens:   {tokens}", f"  requests: {requests}", f"  {free}"]
