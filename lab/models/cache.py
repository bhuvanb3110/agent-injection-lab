"""SQLite response cache keyed by a hash of the request (DECISIONS.md D2, D12, D13).

The key is sha256 over canonical JSON of the whole `ModelRequest`: provider, model ID,
messages, tools, temperature, max_tokens and the repeat index. Canonical means sorted keys and fixed
separators, so two equal requests built with different dict orders get the same key. The API
key is not part of `ModelRequest`, so it can't reach the key or the stored rows.

Only successful responses are stored; errors are never cached.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from lab.models.client import ChatClient
from lab.models.types import ModelRequest, ModelResponse


def canonical_json(request: ModelRequest) -> str:
    return json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def request_key(request: ModelRequest) -> str:
    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            " key TEXT PRIMARY KEY,"
            " request TEXT NOT NULL,"  # canonical JSON, kept so a hit can be audited
            " response TEXT NOT NULL,"
            " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        self._db.commit()

    def get(self, request: ModelRequest) -> ModelResponse | None:
        row = self._db.execute(
            "SELECT response FROM responses WHERE key = ?", (request_key(request),)
        ).fetchone()
        if row is None:
            return None
        # Latency, tokens and cost stay those of the original call; only the flag changes.
        return ModelResponse.model_validate_json(row[0]).model_copy(update={"cached": True})

    def put(self, request: ModelRequest, response: ModelResponse) -> None:
        stored = response.model_copy(update={"cached": False})
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO responses (key, request, response) VALUES (?, ?, ?)",
                (request_key(request), canonical_json(request), stored.model_dump_json()),
            )

    def close(self) -> None:
        self._db.close()


class CachingClient:
    """A ChatClient that answers from the cache when it can and fills it on a miss."""

    def __init__(self, inner: ChatClient, cache: ResponseCache) -> None:
        self.inner = inner
        self.cache = cache

    def complete(self, request: ModelRequest) -> ModelResponse:
        hit = self.cache.get(request)
        if hit is not None:
            return hit
        response = self.inner.complete(request)
        self.cache.put(request, response)
        return response
