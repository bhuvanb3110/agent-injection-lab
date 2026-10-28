"""`lab budget`: daily usage and the next-free time, from the run store alone (no model calls)."""

from __future__ import annotations

from pathlib import Path

from lab.budget import ModelBudget, budgets
from lab.models.providers import load_providers
from lab.scheduler.limits import DAY_S
from lab.scheduler.retry import RequestLogEntry
from lab.store import RunStore

NOW = 1_000_000_000.0
GROQ = "openai/gpt-oss-120b"  # tpd 200,000, rpd 1,000 in configs/providers.yaml


def log(
    store: RunStore, ts: float, tokens: int, provider: str = "groq", model_id: str = GROQ
) -> None:
    store.log_request(
        RequestLogEntry(
            ts=ts, provider=provider, model_id=model_id, status="ok", tokens=tokens
        )
    )


def one(store: RunStore, key: str = f"groq/{GROQ}") -> ModelBudget:
    (row,) = [b for b in budgets(load_providers(), store, NOW) if b.key == key]
    return row


def test_counts_only_the_last_24_hours(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    log(store, NOW - DAY_S - 60, 10_000)  # outside the window
    log(store, NOW - 3600, 30_000)
    log(store, NOW - 60, 20_000)

    row = one(store)

    assert row.tokens_used == 50_000
    assert row.tokens_left == 150_000
    assert row.requests_used == 2
    assert row.requests_left == 998
    store.close()


def test_next_free_time_is_when_the_oldest_usage_leaves_the_window(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    # 180,000 used, so 20,000 left: the next 50,000 needs 30,000 more freed, which the oldest
    # 120,000 (at NOW - 3 h) already covers.
    log(store, NOW - 3 * 3600, 120_000)
    log(store, NOW - 3600, 60_000)

    assert one(store).next_free_at == NOW - 3 * 3600 + DAY_S
    store.close()


def test_next_free_time_is_now_when_the_step_is_already_free(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    log(store, NOW - 60, 10_000)  # 190,000 left, more than the 50,000 step

    assert one(store).next_free_at == NOW
    store.close()


def test_a_model_without_a_daily_token_limit_has_no_next_free_time(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    log(store, NOW - 60, 5_000, provider="ollama", model_id="qwen3:8b")

    row = [b for b in budgets(load_providers(), store, NOW) if b.key == "ollama/qwen3:8b"][0]

    assert row.tokens_limit is None
    assert row.tokens_left is None
    assert row.next_free_at is None
    assert row.tokens_used == 5_000
    store.close()


def test_model_filter_takes_the_key_or_the_bare_id_and_nothing_else(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite")
    providers = load_providers()

    by_key = budgets(providers, store, NOW, f"groq/{GROQ}")
    by_id = budgets(providers, store, NOW, GROQ)
    unknown = budgets(providers, store, NOW, "groq/not-a-model")

    assert [b.key for b in by_key] == [f"groq/{GROQ}"]
    assert [b.key for b in by_id] == [f"groq/{GROQ}"]
    assert unknown == []
    store.close()
