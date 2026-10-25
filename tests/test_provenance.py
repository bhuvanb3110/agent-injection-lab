"""Versions recorded on every run."""

import re

from lab.provenance import agentdojo_version, git_commit


def test_agentdojo_version_is_the_pinned_one() -> None:
    assert agentdojo_version() == "0.1.35"  # pyproject.toml, DECISIONS.md D3


def test_git_commit_is_a_full_hash_with_optional_dirty_flag() -> None:
    assert re.fullmatch(r"[0-9a-f]{40}(-dirty)?", git_commit())
