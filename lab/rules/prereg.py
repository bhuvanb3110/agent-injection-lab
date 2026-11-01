"""PREREGISTRATION.md's machine-readable blocks, and the freeze log (D102, D103, D106, D117).

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
prereg's own hash. It is an ordered list of entries, each carrying the hash of the entry before
it (a hash chain), so that editing, removing or reordering an earlier entry breaks the entry
after it and fails group A. The chain cannot see the last entry being edited or removed: git
history is the audit trail for that. Entries: the six defense freezes, the unblinding of
test-screen and of test-confirm outcomes, the judge's registration and the M6 confirmation
registration.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

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

GENESIS = "genesis"  # the `prev` of the first entry
Scope = Literal["test_screen", "test_confirm"]
SCOPES: tuple[Scope, ...] = ("test_screen", "test_confirm")


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{value!r} is not an ISO 8601 date and time") from error
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{value!r} must be a full UTC date and time, e.g. 2026-11-25T14:03:00Z")
    return value.astimezone(UTC)


def _check_hash(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("not a lowercase hex SHA-256")
    return value


class _Entry(_Strict):
    # `genesis` for the first entry, otherwise the entry_hash of the entry before it.
    prev: str

    @field_validator("prev")
    @classmethod
    def _prev(cls, value: str) -> str:
        return value if value == GENESIS else _check_hash(value)


class Freeze(_Entry):
    """One defense configuration frozen: its file and the file's hash, before its first
    test-screen run."""

    kind: Literal["freeze"]
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
        return _check_hash(value)


class Unblinding(_Entry):
    """The dated record that lets the outcomes of a blinded set be read (D103). `test_screen`:
    valid once all six defenses are frozen. `test_confirm`: valid once the M6 confirmation is
    registered."""

    kind: Literal["unblinding"]
    scope: Scope
    recorded_at: datetime
    note: str = ""

    @field_validator("recorded_at", mode="before")
    @classmethod
    def _date(cls, value: Any) -> datetime:
        return _utc(value)


class JudgeRegistration(_Entry):
    """The judge's registration (D105): committed before it scores any test output."""

    kind: Literal["judge"]
    registered_at: datetime
    code_sha256: str
    labels_sha256: str
    kappa: float

    @field_validator("registered_at", mode="before")
    @classmethod
    def _date(cls, value: Any) -> datetime:
        return _utc(value)


class Confirmation(_Entry):
    """The planned M6 registration (D106): which configurations the test-confirm cases will be
    run with, written down after the test-screen outcomes were unblinded and before any
    test-confirm run. `selection` is a committed file that says so, pinned here by hash."""

    kind: Literal["confirmation"]
    registered_at: datetime
    selection: str  # repository-relative path
    selection_sha256: str
    note: str = ""

    @field_validator("registered_at", mode="before")
    @classmethod
    def _date(cls, value: Any) -> datetime:
        return _utc(value)

    @field_validator("selection_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _check_hash(value)


AnyEntry = Freeze | Unblinding | JudgeRegistration | Confirmation
_ENTRY: TypeAdapter[AnyEntry] = TypeAdapter(
    Annotated[AnyEntry, Field(discriminator="kind")]
)


def entry_time(entry: AnyEntry) -> datetime:
    if isinstance(entry, Freeze):
        return entry.frozen_at
    if isinstance(entry, Unblinding):
        return entry.recorded_at
    return entry.registered_at


def entry_hash(entry: AnyEntry) -> str:
    """SHA-256 of the entry's canonical JSON (sorted keys, no spaces, times as UTC with a Z).
    It covers `prev`, so it covers the whole chain before it. It does not depend on how the YAML
    is formatted, only on what the entry says."""
    body = json.dumps(entry.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def link(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For whoever appends to the log: the entries (as dicts, with no `prev` or a stale one) with
    `prev` filled in down the chain, ready to be written under `entries:`."""
    linked: list[dict[str, Any]] = []
    prev = GENESIS
    for raw in entries:
        entry = {**raw, "prev": prev}
        prev = entry_hash(_ENTRY.validate_python(entry))
        linked.append(entry)
    return linked


class FreezeLog(_Strict):
    entries: list[Annotated[AnyEntry, Field(discriminator="kind")]] = Field(default_factory=list)

    @property
    def freezes(self) -> list[Freeze]:
        return [e for e in self.entries if isinstance(e, Freeze)]

    def unblindings(self, scope: Scope) -> list[Unblinding]:
        return [e for e in self.entries if isinstance(e, Unblinding) and e.scope == scope]

    def unblinding(self, scope: Scope) -> Unblinding | None:
        found = self.unblindings(scope)
        return found[0] if found else None

    @property
    def judges(self) -> list[JudgeRegistration]:
        return [e for e in self.entries if isinstance(e, JudgeRegistration)]

    @property
    def judge(self) -> JudgeRegistration | None:
        return self.judges[0] if self.judges else None

    @property
    def confirmations(self) -> list[Confirmation]:
        return [e for e in self.entries if isinstance(e, Confirmation)]

    @property
    def confirmation(self) -> Confirmation | None:
        return self.confirmations[0] if self.confirmations else None


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


def chain_problems(log: FreezeLog) -> list[str]:
    """The hash chain, and the order in time. An entry whose `prev` is not the hash of the entry
    before it means an earlier entry was edited, removed or reordered (or this one was written
    wrongly); each such entry is reported with the `prev` it should have."""
    problems = []
    expected = GENESIS
    previous: AnyEntry | None = None
    for number, entry in enumerate(log.entries, start=1):
        if entry.prev != expected:
            problems.append(
                f"entry {number} ({entry.kind}): prev is {entry.prev}, expected {expected}: "
                "an earlier entry was edited, removed or reordered"
            )
        if previous is not None and entry_time(entry) < entry_time(previous):
            problems.append(f"entry {number} ({entry.kind}) is dated before entry {number - 1}")
        expected, previous = entry_hash(entry), entry
    return problems


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


def unblinding_problems(log: FreezeLog, root: Path, scope: Scope = "test_screen") -> list[str]:
    """Why the unblinding record of `scope`, if there is one, is not valid.

    `test_screen`: valid only when all six defenses are frozen, each at or before the record
    (D103). `test_confirm`: valid only when the M6 confirmation is registered and valid, at or
    before the record."""
    record = log.unblinding(scope)
    if record is None:
        return []
    at = record.recorded_at
    problems: list[str] = []
    if scope == "test_screen":
        frozen, _ = freeze_problems(log, root)
        missing = [d for d in DEFENSES if d not in frozen]
        late = [d for d in DEFENSES if d in frozen and frozen[d].frozen_at > at]
        if missing:
            problems.append(f"defenses not frozen: {', '.join(missing)}")
        if late:
            problems.append(f"defenses frozen after the record: {', '.join(late)}")
        return problems
    confirmation = log.confirmation
    if confirmation is None:
        return ["there is no confirmation registration"]
    problems = confirmation_problems(log, root)
    if confirmation.registered_at > at:
        problems.append("the confirmation was registered after the record")
    return problems


def confirmation_problems(log: FreezeLog, root: Path) -> list[str]:
    """Why the M6 confirmation record, if there is one, is not valid: its selection file must
    exist with the recorded hash, there must be exactly one such record, and it must come after a
    valid unblinding of the test-screen outcomes (M6 picks configurations from screening)."""
    records = log.confirmations
    if not records:
        return []
    problems = []
    if len(records) > 1:
        problems.append(f"{len(records)} confirmation records; there is one")
    record = records[0]
    path_problem = relative_path_problem(record.selection)
    if path_problem is not None:
        problems.append(path_problem)
    elif not (root / record.selection).is_file():
        problems.append(f"{record.selection} does not exist")
    elif sha256_file(root / record.selection) != record.selection_sha256:
        problems.append(f"{record.selection} changed after it was registered")
    screen = log.unblinding("test_screen")
    if screen is None or unblinding_problems(log, root, "test_screen"):
        problems.append("the test-screen outcomes are not validly unblinded before it")
    elif record.registered_at < screen.recorded_at:
        problems.append("registered before the test-screen unblinding record")
    return problems


def log_problems(log: FreezeLog, root: Path) -> list[str]:
    """Everything wrong with the log: the chain, the order, every freeze, both unblinding
    records, the confirmation and the judge."""
    problems = chain_problems(log)
    problems += freeze_problems(log, root)[1]
    for scope in SCOPES:
        count = len(log.unblindings(scope))
        if count > 1:
            problems.append(f"{count} {scope} unblinding records; there is one")
        problems += [f"{scope} unblinding record: {p}"
                     for p in unblinding_problems(log, root, scope)]
    problems += [f"confirmation: {p}" for p in confirmation_problems(log, root)]
    if len(log.judges) > 1:
        problems.append(f"{len(log.judges)} judge registrations; there is one")
    if log.judge is not None and log.judge.kappa < JUDGE_MIN_KAPPA:
        problems.append(f"judge registration: kappa {log.judge.kappa} is below {JUDGE_MIN_KAPPA}")
    return problems


def usable_log(root: Path) -> tuple[FreezeLog | None, str]:
    """The log, or why it can not be relied on (absent, malformed, chain broken)."""
    try:
        log = load_freeze_log(root)
    except PreregError as error:
        return None, str(error)
    if log is None:
        return None, f"there is no {FREEZE_LOG_NAME}"
    chain = chain_problems(log)
    if chain:
        return None, f"the {FREEZE_LOG_NAME} chain is broken ({chain[0]})"
    return log, ""


@dataclass(frozen=True)
class Blinding:
    """Whether the outcomes of a blinded set (test-screen, or test-confirm) may be shown."""

    unblinded: bool
    reason: str
    recorded_at: datetime | None  # the unblinding record's time, if there is one


def blinding_state(root: Path, scope: Scope = "test_screen") -> Blinding:
    """Blinded until the freeze log holds a valid dated unblinding record for `scope`. A record
    that is not valid is refused, not half-honoured: the outcomes stay hidden."""
    log, why = usable_log(root)
    if log is None:
        return Blinding(False, why, None)
    record = log.unblinding(scope)
    if record is None:
        return Blinding(False, f"{FREEZE_LOG_NAME} has no {scope} unblinding record", None)
    at = record.recorded_at
    problems = unblinding_problems(log, root, scope)
    if problems:
        return Blinding(
            False,
            f"the {scope} unblinding record of {at:%Y-%m-%d %H:%M} UTC is refused "
            f"({'; '.join(problems)})",
            at,
        )
    return Blinding(True, f"unblinded by the record of {at:%Y-%m-%d %H:%M} UTC", at)


def confirmation_state(root: Path) -> tuple[bool, str]:
    """Whether the test-confirm cases may be run: the freeze log holds a valid M6 confirmation
    registration. Returns (registered, why not)."""
    log, why = usable_log(root)
    if log is None:
        return False, why
    if log.confirmation is None:
        return False, f"{FREEZE_LOG_NAME} has no confirmation registration"
    problems = confirmation_problems(log, root)
    if problems:
        return False, f"the confirmation registration is refused ({'; '.join(problems)})"
    return True, ""
