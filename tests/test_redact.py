"""Redaction of key-like text in stored provider error messages (D42)."""

import pytest

from lab.redact import REDACTED, redact


@pytest.mark.parametrize(
    "secret",
    [
        "gsk_AbCdEf0123456789XyZaBcDeF0123456789",  # Groq
        "sk-proj-AbCdEf0123456789XyZ",  # OpenAI
        "ghp_AbCdEf0123456789XyZaBcDeF012345",  # GitHub
        "github_pat_11ABCDEFG0123456789_abcdefghij",
        "hf_AbCdEf0123456789XyZaBc",  # Hugging Face
        "AIzaSyAbCdEf0123456789XyZaBcDeF012345",  # Google
        "q8Zr2Lm7Xp4Vt9Kd3Nw6Hb1Yc5Fg0Js8Ua2Ee",  # no known prefix, but long and mixed
    ],
)
def test_key_shaped_values_are_removed(secret: str) -> None:
    text = f"Incorrect API key provided: {secret}. You can find your key at console."
    assert secret not in redact(text)
    assert REDACTED in redact(text)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        ('{"api_key": "shortish1"}', "shortish1"),
        ("password=hunter22", "hunter22"),
        ("access-token: tok3n", "tok3n"),
    ],
)
def test_named_secrets_and_bearer_tokens_are_removed(text: str, secret: str) -> None:
    assert secret not in redact(text)


def test_exact_secret_is_removed_even_when_it_looks_harmless() -> None:
    # A key with no recognisable shape is still caught when the caller passes it.
    secret = "plain words key"
    assert redact(f"bad key: {secret}!", secrets=[secret]) == f"bad key: {REDACTED}!"
    # Very short "secrets" are not replaced verbatim; they would wreck the text.
    assert redact("a bank account", secrets=["a"]) == "a bank account"


def test_ordinary_error_messages_are_left_alone() -> None:
    # A Groq tokens/day 429 and a 400: nothing in them is key-like.
    messages = [
        "HTTP 429 from groq: Rate limit reached for model `openai/gpt-oss-20b` on tokens per day "
        "(TPD): Limit 200000, Used 199021, Requested 2342. Please try again in 12m3.5s.",
        "HTTP 400 from groq: 'messages.2' : for 'role:tool' the following must be satisfied",
        "APITimeoutError: Request timed out.",
    ]
    for message in messages:
        assert redact(message) == message
