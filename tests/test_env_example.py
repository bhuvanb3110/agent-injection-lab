"""`.env.example` is committed, so it may only hold empty placeholders, never a real key (D31)."""

from pathlib import Path

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_env_example_has_only_empty_placeholders() -> None:
    entries = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, value = stripped.partition("=")
        assert sep == "=", f"not a NAME= line: {line!r}"
        assert name.removeprefix("export ").strip().isidentifier(), f"bad name: {line!r}"
        # Report the name only: if a key ever lands here, the test output must not repeat it.
        assert value.strip() == "", f"{name.strip()} has a value; .env.example must stay empty"
        entries.append(name.strip())
    assert "GROQ_API_KEY" in entries
