"""Versions recorded on every run (CLAUDE.md: pin and record versions)."""

from __future__ import annotations

import subprocess
from importlib.metadata import version


def agentdojo_version() -> str:
    return version("agentdojo")


def git_commit() -> str:
    """HEAD's hash, with "-dirty" if tracked files have uncommitted changes."""
    head = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    return f"{head}-dirty" if dirty else head


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()
