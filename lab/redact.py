"""Remove anything key-like from text before it is stored (DECISIONS.md D42).

Provider error messages are stored with failed requests. They come from the provider, not from
our request, but some providers echo part of the key back ("Incorrect API key provided: gsk_..."),
so every stored message goes through `redact` first. It errs towards removing too much: a
redacted request ID costs nothing, a leaked key does.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

_PATTERNS = [
    # "Bearer <token>", as in an echoed Authorization header.
    (re.compile(r"(?i)\b(bearer\s+)[^\s\"',;]+"), rf"\1{REDACTED}"),
    # key=value or "key": "value" where the name says it is secret.
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|access[_-]?token|secret|password|authorization)"
            r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]{4,}"
        ),
        rf"\1{REDACTED}",
    ),
    # Known key prefixes: Groq gsk_, OpenAI sk-, GitHub ghp_/github_pat_, Hugging Face hf_,
    # Google AIza.
    (
        re.compile(
            r"\b(?:gsk_|sk-|sk_|ghp_|gho_|ghs_|github_pat_|hf_|xai-)[A-Za-z0-9_\-]{8,}"
            r"|\bAIza[0-9A-Za-z_\-]{20,}"
        ),
        REDACTED,
    ),
    # Any other long run of letters and digits (32+ characters, both kinds present).
    (
        re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b"),
        REDACTED,
    ),
]

# A secret shorter than this is not looked for verbatim: replacing every "a" would wreck the text.
_MIN_SECRET_LEN = 8


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """`text` with the given secrets, and anything shaped like a key, replaced by [REDACTED]."""
    for secret in secrets:
        if len(secret) >= _MIN_SECRET_LEN:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
