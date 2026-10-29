"""PREREGISTRATION.md's machine-readable blocks, and the freeze log (D102, D103, D106, D116).

The file formats are in docs/PREREG_FORMAT.md; this module is what reads them. Step 11 writes
PREREGISTRATION.md; nothing here writes it.

**The pin block.** PREREGISTRATION.md holds exactly one fenced block whose opening line is
`yaml pin-block` (three backticks, then those two words). Its body is a mapping from a
repository-relative path (forward slashes) to the lowercase hex SHA-256 of that file's raw
bytes. Raw bytes, so a line-ending change is a change; `.gitattributes` fixes every text file
to LF.

**The values block.** Exactly one more block, `yaml prereg-values`, holds the values the prereg
states that live in files that cannot be pinned whole. Today: the sampling settings
(temperature, reasoning effort) of each model, which `configs/providers.yaml` also holds next to
limits that are re-checked before every run.

**The freeze log**, `FREEZE_LOG.yaml`, is a separate file so that a freeze does not change the
prereg's own hash (every M4 run carries it). It is append-only by convention: git history is the
audit trail. It holds the six defense freezes, the unblinding record and the judge's
registration.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PREREG_NAME = "PREREGISTRATION.md"
FREEZE_LOG_NAME = "FREEZE_LOG.yaml"
PIN_BLOCK_TAG = "pin-block"
VALUES_BLOCK_TAG = "prereg-values"

# The six defense configurations of the defense sweep (PLAN_M3 §0, D99, D108). "none" is T2.
# "deterministic" is T4 = policy_v1, frozen long before the others (D108): its freeze record
# points at the policy file.
DEFENSES = ("prompt", "detection", "architecture", "deterministic", "arch_det", "everything")
Defense = Literal["prompt", "detection", "architecture", "deterministic", "arch_det", "everything"]
DEFENSE_CONFIG_DIR = "configs/defenses"
# The judge may score a test output only if it agreed with the hand labels this well (D105).
JUDGE_MIN_KAPPA = 0.6

_SHA256 = re.compile(r"[0-9a-f]{64}")


class PreregError(ValueError):
    """PREREGISTRATION.md or the freeze log is missing something or malformed."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def prereg_path(root: Path) -> Path:
    return root / PREREG_NAME


def prereg_sha256(root: Path) -> str | None:
    """The hash `prereg_sha256` on a run record holds: SHA-256 of PREREGISTRATION.md's raw bytes.
    None if the file does not exist."""
    path = prereg_path(root)
    return sha256_file(path) if path.is_file() else None


# --- fenced blocks -------------------------------------------------------------------------


def fenced_blocks(text: str) -> list[tuple[str, str]]:
    """(info string, body) of every fenced block that opens with three backticks at column 0."""
    blocks: list[tuple[str, str]] = []
    info: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if info is None:
            if line.startswith("```"):
                info, body = line[3:].strip(), []
        elif line.rstrip() == "```":
            blocks.append((info, "\n".join(body)))
            info = None
        else:
            body.append(line)
    if info is not None:
        raise PreregError(f"the fenced block opened with ```{info} is never closed")
    return blocks


def _block(text: str, tag: str) -> Any:
    found = [body for info, body in fenced_blocks(text) if info.split() == ["yaml", tag]]
    if len(found) != 1:
        raise PreregError(f"{PREREG_NAME} must hold exactly one `yaml {tag}` block; found "
                          f"{len(found)}")
    try:
        return yaml.safe_load(found[0])
    except yaml.YAMLError as error:
        raise PreregError(f"the {tag} block is not valid YAML: {error}") from error


def _prereg_text(root: Path) -> str:
    path = prereg_path(root)
    if not path.is_file():
        raise PreregError(f"{PREREG_NAME} does not exist")
    return path.read_text(encoding="utf-8")


def relative_path_problem(path: str) -> str | None:
    """Why `path` is not a repository-relative path with forward slashes, or None."""
    parts = path.split("/")
    if path.startswith("/") or "\\" in path or ".." in parts or "" in parts:
        return f"{path!r} is not a repository-relative path with forward slashes"
    return None


def load_pins(root: Path) -> dict[str, str]:
    """The pin block: repository-relative path -> SHA-256 of the file's raw bytes."""
    data = _block(_prereg_text(root), PIN_BLOCK_TAG)
    if not isinstance(data, dict) or not data:
        raise PreregError(f"the {PIN_BLOCK_TAG} block must be a non-empty mapping path -> sha256")
    pins: dict[str, str] = {}
    for path, digest in data.items():
        if not isinstance(path, str) or not isinstance(digest, str):
            raise PreregError(
                f"{PIN_BLOCK_TAG}: {path!r}: {digest!r} is not a path and a hash string (quote a "
                "hash that is all digits, or YAML reads it as a number)"
            )
        problem = relative_path_problem(path)
        if problem is not None:
            raise PreregError(f"{PIN_BLOCK_TAG}: {problem}")
        if not _SHA256.fullmatch(digest):
            raise PreregError(f"{PIN_BLOCK_TAG}: {path}: {digest!r} is not a lowercase hex "
                              "SHA-256")
        pins[path] = digest
    return pins


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelSampling(_Strict):
    temperature: float
    reasoning_effort: Literal["low", "medium", "high"] | None


class PreregValues(_Strict):
    # "<provider>/<model id>" -> its sampling settings, e.g. "groq/openai/gpt-oss-20b".
    sampling: dict[str, ModelSampling]


def load_values(root: Path) -> PreregValues:
    data = _block(_prereg_text(root), VALUES_BLOCK_TAG)
    try:
        return PreregValues.model_validate(data)
    except ValidationError as error:
        raise PreregError(f"the {VALUES_BLOCK_TAG} block is malformed: {error}") from error


# --- the freeze log ------------------------------------------------------------------------


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{value!r} is not an ISO 8601 date and time") from error
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{value!r} must be a full UTC date and time, e.g. 2026-11-25T14:03:00Z")
    return value.astimezone(UTC)


class Freeze(_Strict):
    """One defense configuration frozen: its file and the file's hash, before its first
    test-screen run."""

    defense: Defense
    config: str  # repository-relative path
    sha256: str
    frozen_at: datetime

    @field_validator("frozen_at", mode="before")
    @classmethod
    def _date(cls, value: Any) -> datetime:
        return _utc(value)

    @field_validator("sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("not a lowercase hex SHA-256")
        return value


class Unblinding(_Strict):
    """The dated record that lets test-screen outcomes be read (D103)."""

    recorded_at: datetime
    note: str = ""

    @field_validator("recorded_at", mode="before")
    @classmethod
    def _date(cls, value: Any) -> datetime:
        return _utc(value)


class JudgeRegistration(_Strict):
    """The judge's registration (D105): committed before it scores any test output."""

    registered_at: datetime
    code_sha256: str
    labels_sha256: str
    kappa: float

    @field_validator("registered_at", mode="before")
    @classmethod
    def _date(cls, value: Any) -> datetime:
        return _utc(value)


class FreezeLog(_Strict):
    freezes: list[Freeze] = Field(default_factory=list)
    unblinding: Unblinding | None = None
    judge: JudgeRegistration | None = None


def load_freeze_log(root: Path) -> FreezeLog | None:
    """The freeze log, or None if the file does not exist."""
    path = root / FREEZE_LOG_NAME
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return FreezeLog.model_validate(data)
    except (yaml.YAMLError, ValidationError) as error:
        raise PreregError(f"{FREEZE_LOG_NAME} is malformed: {error}") from error


def freeze_problems(log: FreezeLog, root: Path) -> tuple[dict[str, Freeze], list[str]]:
    """The valid freezes by defense, and a message for each record that is not valid.

    A record is valid if its defense appears once, its config file exists under the repository
    and still has the recorded hash (a config edited after its freeze is no longer frozen).
    """
    valid: dict[str, Freeze] = {}
    problems: list[str] = []
    seen: dict[str, int] = {}
    for freeze in log.freezes:
        seen[freeze.defense] = seen.get(freeze.defense, 0) + 1
    for freeze in log.freezes:
        name = freeze.defense
        path_problem = relative_path_problem(freeze.config)
        if path_problem is not None:
            problems.append(f"{name}: {path_problem}")
        elif seen[name] > 1:
            problems.append(f"{name}: frozen {seen[name]} times; a re-freeze is an amendment, "
                            "which this log does not take")
        elif not (root / freeze.config).is_file():
            problems.append(f"{name}: {freeze.config} does not exist")
        elif sha256_file(root / freeze.config) != freeze.sha256:
            problems.append(f"{name}: {freeze.config} changed after it was frozen")
        else:
            valid[name] = freeze
    return valid, problems


@dataclass(frozen=True)
class Blinding:
    """Whether test-screen outcomes may be shown (D103)."""

    unblinded: bool
    reason: str
    recorded_at: datetime | None  # the unblinding record's time, if there is one


def unblinding_problems(log: FreezeLog, root: Path) -> list[str]:
    """Why the unblinding record, if there is one, is not valid: it is valid only when all six
    defenses are frozen, each at or before the record (D103)."""
    if log.unblinding is None:
        return []
    frozen, _ = freeze_problems(log, root)
    at = log.unblinding.recorded_at
    missing = [d for d in DEFENSES if d not in frozen]
    late = [d for d in DEFENSES if d in frozen and frozen[d].frozen_at > at]
    problems = []
    if missing:
        problems.append(f"defenses not frozen: {', '.join(missing)}")
    if late:
        problems.append(f"defenses frozen after the record: {', '.join(late)}")
    return problems


def blinding_state(root: Path) -> Blinding:
    """Blinded until the freeze log holds a dated unblinding record that is valid. A record that
    is not valid is refused, not half-honoured: the outcomes stay hidden."""
    try:
        log = load_freeze_log(root)
    except PreregError as error:
        return Blinding(False, str(error), None)
    if log is None:
        return Blinding(False, f"there is no {FREEZE_LOG_NAME}", None)
    if log.unblinding is None:
        return Blinding(False, f"{FREEZE_LOG_NAME} has no unblinding record", None)
    at = log.unblinding.recorded_at
    problems = unblinding_problems(log, root)
    if problems:
        return Blinding(
            False,
            f"the unblinding record of {at:%Y-%m-%d %H:%M} UTC is refused ({'; '.join(problems)})",
            at,
        )
    return Blinding(True, f"unblinded by the record of {at:%Y-%m-%d %H:%M} UTC", at)
