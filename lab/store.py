"""SQLite run store: one row per scored run, its model calls, and every provider request.

Three tables (DECISIONS.md D2, D25):
- `runs`: one row per run that has an outcome, with every field in BRIEF §9 plus the versions,
  sampling settings and repeat index. A run that hit an InfraError or a daily cap has no row:
  it was never scored, and stays pending for the next invocation.
- `calls`: the per-call records of each run (tokens, latency, cost, cache hit, attempts).
- `requests`: every real request sent to a provider, including failed attempts. The rate
  limiter reads it back on start-up so daily usage survives a restart.

Rows are only inserted, never updated: a run ID that is already present is a bug, not a retry.
The one exception is a run's failure label, an annotation added after scoring (D43).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from lab.models.types import CallRecord
from lab.scheduler.retry import RequestLogEntry
from lab.scheduler.spec import RunSpec


class RunRecord(BaseModel):
    run_id: str
    spec: RunSpec
    config_hash: str
    git_commit: str
    agentdojo_version: str
    seed: int | None  # the seed sent on every call of the run (D27); None: none was sent
    outcome: str  # solved, not_solved, max_iterations, no_final_answer, model_error
    outcome_subtype: str | None  # for model_error: http_400, no_choices, ... (D21)
    utility: bool  # True only for solved; the headline number
    state_check_passed: bool | None  # None when the run never finished (model_error)
    error: str | None
    failure_label: str | None = None  # hand label from configs/labels/ (D43); None: unlabeled
    calls: list[CallRecord]
    malformed_tool_calls: int
    messages: list[dict[str, Any]]  # full trace, JSON-safe
    started_at: str  # ISO 8601 UTC; also the run's date
    finished_at: str
    wall_latency_s: float

    # Totals derived from `calls`. A total is None if any call lacks that number: never guessed.
    @property
    def input_tokens(self) -> int | None:
        return _total([c.input_tokens for c in self.calls])

    @property
    def output_tokens(self) -> int | None:
        return _total([c.output_tokens for c in self.calls])

    @property
    def reasoning_tokens(self) -> int | None:
        return _total([c.reasoning_tokens for c in self.calls])

    @property
    def cost_usd(self) -> float | None:
        costs = [c.cost_usd for c in self.calls]
        return None if None in costs else sum(c for c in costs if c is not None)

    @property
    def requests_sent(self) -> int:
        return sum(c.attempts for c in self.calls)


def _total(values: Sequence[int | None]) -> int | None:
    return None if None in values else sum(v for v in values if v is not None)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    experiment TEXT NOT NULL,
    spec TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    agentdojo_version TEXT NOT NULL,
    benchmark_version TEXT NOT NULL,
    suite TEXT NOT NULL,
    user_task_id TEXT NOT NULL,
    topology TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    temperature REAL NOT NULL,
    reasoning_effort TEXT,
    max_tokens INTEGER,
    seed INTEGER,
    repeat_idx INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    outcome_subtype TEXT,
    utility INTEGER NOT NULL,
    state_check_passed INTEGER,
    error TEXT,
    failure_label TEXT,
    n_calls INTEGER NOT NULL,
    n_cached_calls INTEGER NOT NULL,
    requests_sent INTEGER NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cost_usd REAL,
    malformed_tool_calls INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    wall_latency_s REAL NOT NULL,
    messages TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    idx INTEGER NOT NULL,
    record TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    cached INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    latency_s REAL NOT NULL,
    cost_usd REAL,
    malformed_tool_calls INTEGER NOT NULL,
    PRIMARY KEY (run_id, idx)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    status TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    error_type TEXT,
    http_status INTEGER,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS requests_by_model ON requests (provider, model_id, ts);
"""

# Columns added after the first stores were written, as (table, column, type). A store created
# before then gets them added on open; old rows keep NULL, since the values were never recorded.
_ADDED_COLUMNS = [
    ("requests", "error_type", "TEXT"),  # D42
    ("requests", "http_status", "INTEGER"),  # D42
    ("requests", "error_message", "TEXT"),  # D42
]


class RunStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        for table, column, sql_type in _ADDED_COLUMNS:
            existing = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        self._db.commit()

    # --- runs ---

    def has_run(self, run_id: str) -> bool:
        row = self._db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return row is not None

    def save_run(self, record: RunRecord) -> None:
        """Insert a run and its calls in one transaction."""
        spec = record.spec
        row = {
            "run_id": record.run_id,
            "experiment": spec.experiment,
            "spec": spec.model_dump_json(),
            "config_hash": record.config_hash,
            "git_commit": record.git_commit,
            "agentdojo_version": record.agentdojo_version,
            "benchmark_version": spec.benchmark_version,
            "suite": spec.suite,
            "user_task_id": spec.user_task_id,
            "topology": spec.topology,
            "provider": spec.provider,
            "model_id": spec.model_id,
            "temperature": spec.temperature,
            "reasoning_effort": spec.reasoning_effort,
            "max_tokens": spec.max_tokens,
            "seed": record.seed,
            "repeat_idx": spec.repeat_idx,
            "outcome": record.outcome,
            "outcome_subtype": record.outcome_subtype,
            "utility": record.utility,
            "state_check_passed": record.state_check_passed,
            "error": record.error,
            "failure_label": record.failure_label,
            "n_calls": len(record.calls),
            "n_cached_calls": sum(c.cached for c in record.calls),
            "requests_sent": record.requests_sent,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "reasoning_tokens": record.reasoning_tokens,
            "cost_usd": record.cost_usd,
            "malformed_tool_calls": record.malformed_tool_calls,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "wall_latency_s": record.wall_latency_s,
            "messages": json.dumps(record.messages),
        }
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        with self._db:
            self._db.execute(f"INSERT INTO runs ({columns}) VALUES ({placeholders})", row)
            self._db.executemany(
                "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        record.run_id,
                        idx,
                        call.model_dump_json(),
                        call.provider,
                        call.model_id,
                        call.cached,
                        call.attempts,
                        call.input_tokens,
                        call.output_tokens,
                        call.reasoning_tokens,
                        call.latency_s,
                        call.cost_usd,
                        call.malformed_tool_calls,
                    )
                    for idx, call in enumerate(record.calls)
                ],
            )

    def get_run(self, run_id: str) -> RunRecord:
        row = self._db.execute(
            "SELECT spec, config_hash, git_commit, agentdojo_version, seed, outcome,"
            " outcome_subtype, utility, state_check_passed, error, failure_label,"
            " malformed_tool_calls, messages, started_at, finished_at, wall_latency_s"
            " FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        calls = [
            CallRecord.model_validate_json(r[0])
            for r in self._db.execute(
                "SELECT record FROM calls WHERE run_id = ? ORDER BY idx", (run_id,)
            )
        ]
        return RunRecord(
            run_id=run_id,
            spec=RunSpec.model_validate_json(row[0]),
            config_hash=row[1],
            git_commit=row[2],
            agentdojo_version=row[3],
            seed=row[4],
            outcome=row[5],
            outcome_subtype=row[6],
            utility=bool(row[7]),
            state_check_passed=None if row[8] is None else bool(row[8]),
            error=row[9],
            failure_label=row[10],
            malformed_tool_calls=row[11],
            messages=json.loads(row[12]),
            started_at=row[13],
            finished_at=row[14],
            wall_latency_s=row[15],
            calls=calls,
        )

    def set_failure_label(self, run_id: str, label: str | None) -> None:
        """The one update the store allows: a failure label is an annotation added after the
        run was scored (D43). It never touches the outcome, utility or anything measured."""
        with self._db:
            cursor = self._db.execute(
                "UPDATE runs SET failure_label = ? WHERE run_id = ?", (label, run_id)
            )
        if cursor.rowcount != 1:
            raise KeyError(run_id)

    def run_ids(self, experiment: str) -> list[str]:
        rows = self._db.execute(
            "SELECT run_id FROM runs WHERE experiment = ? ORDER BY started_at, run_id",
            (experiment,),
        )
        return [r[0] for r in rows]

    def runs(self, experiment: str) -> list[RunRecord]:
        return [self.get_run(run_id) for run_id in self.run_ids(experiment)]

    # --- provider requests (rate-limit accounting) ---

    def log_request(self, entry: RequestLogEntry) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO requests (ts, provider, model_id, status, tokens, error_type,"
                " http_status, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.ts,
                    entry.provider,
                    entry.model_id,
                    entry.status,
                    entry.tokens,
                    entry.error_type,
                    entry.http_status,
                    entry.error_message,
                ),
            )

    def requests(self, provider: str, model_id: str) -> list[RequestLogEntry]:
        rows = self._db.execute(
            "SELECT ts, provider, model_id, status, tokens, error_type, http_status,"
            " error_message FROM requests WHERE provider = ? AND model_id = ? ORDER BY id",
            (provider, model_id),
        )
        return [RequestLogEntry(*row) for row in rows]

    def request_history(
        self, provider: str, model_id: str, since: float
    ) -> list[tuple[float, int]]:
        """(time, tokens) of every request to this model since `since`, oldest first."""
        rows = self._db.execute(
            "SELECT ts, tokens FROM requests WHERE provider = ? AND model_id = ? AND ts > ?"
            " ORDER BY ts",
            (provider, model_id, since),
        )
        return [(r[0], r[1]) for r in rows]

    def request_statuses(self, provider: str, model_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT status FROM requests WHERE provider = ? AND model_id = ? ORDER BY id",
            (provider, model_id),
        )
        return [r[0] for r in rows]

    def close(self) -> None:
        self._db.close()
