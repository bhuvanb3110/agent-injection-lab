import socket
from collections.abc import Callable
from pathlib import Path

import pytest

from lab.models.types import CallRecord
from lab.scheduler.spec import RunSpec
from lab.store import RunRecord


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a network connection (DECISIONS.md D10)."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Network access is disabled in tests; use the fake model.")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def no_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """For plumbing tests that run scripted FakeModels through `lab run` on an M2-style `attack:`
    config. Some of its (user task, injection task) pairs are test-split cases, which the guard
    (lab.rules.guard, D102) refuses whatever the model. These tests are about something else;
    the guard's own tests (tests/test_guard.py) do not use this."""
    monkeypatch.setattr("lab.cli.check_run", lambda *args, **kwargs: None)


# --- a milestone-1 run record, shared by the store tests -----------------------------------
# Fixtures rather than module-level helpers, so no test file has to import from another.

MakeCall = Callable[..., CallRecord]
MakeRecord = Callable[[list[CallRecord]], RunRecord]


@pytest.fixture
def run_spec() -> RunSpec:
    """A benign milestone-1 spec: no attack fields, so its run ID is computed as in M1 (D48)."""
    return RunSpec(
        experiment="test",
        suite="banking",
        benchmark_version="v1.2.2",
        user_task_id="user_task_0",
        topology="t1",
        provider="groq",
        model_id="openai/gpt-oss-20b",
        temperature=0.6,
        reasoning_effort="medium",
        max_tokens=None,
        seed=1002,
        repeat_idx=2,
    )


@pytest.fixture
def make_call() -> MakeCall:
    """A CallRecord with usage numbers set; keyword arguments override any field."""

    def make(**overrides: object) -> CallRecord:
        fields: dict[str, object] = {
            "provider": "groq",
            "model_id": "openai/gpt-oss-20b",
            "request_key": "abc",
            "cached": False,
            "attempts": 2,
            "input_tokens": 100,
            "output_tokens": 30,
            "reasoning_tokens": 12,
            "latency_s": 0.5,
            "cost_usd": 0.001,
            "finish_reason": "stop",
            "malformed_tool_calls": 1,
        }
        return CallRecord(**(fields | overrides))  # type: ignore[arg-type]

    return make


@pytest.fixture
def make_record(run_spec: RunSpec) -> MakeRecord:
    """A solved run of `run_spec` with the given calls, as milestone 1 stored it."""

    def make(calls: list[CallRecord]) -> RunRecord:
        return RunRecord(
            run_id=run_spec.run_id,
            spec=run_spec,
            config_hash="cfg",
            git_commit="deadbeef",
            agentdojo_version="0.1.35",
            seed=run_spec.seed,
            outcome="solved",
            outcome_subtype=None,
            utility=True,
            state_check_passed=True,
            error=None,
            calls=calls,
            malformed_tool_calls=sum(c.malformed_tool_calls for c in calls),
            messages=[{"role": "user", "content": [{"type": "text", "content": "Pay my bill."}]}],
            started_at="2026-10-25T00:00:00+00:00",
            finished_at="2026-10-25T00:00:03+00:00",
            wall_latency_s=3.0,
        )

    return make


# --- a fixture repository for the validate-rules tests (PLAN_M3 step 10) ----------------------
# A copy of the files group A reads, with a PREREGISTRATION.md that pins them. Tests then tamper
# with the copy, never with the real files. Fixtures rather than helpers, as above.

PROJECT = Path(__file__).resolve().parents[1]
FIXTURE_TREES = (
    "data/attack_cases", "configs/golden", "configs/policies", "configs/humans",
    "configs/labels", "configs/experiments",
)
FIXTURE_FILES = ("configs/providers.yaml",)
Pins = dict[str, str]
WritePrereg = Callable[..., Path]


@pytest.fixture(scope="session")
def regenerated() -> dict[str, str]:
    """What the dataset generator writes, once per session (it takes a few seconds). The copy of
    the data in a fixture repository is the real data, so this is what it must equal."""
    from lab.attacks.dataset import BANKING, build

    return build(BANKING, PROJECT / "data" / "attack_cases")[2]


@pytest.fixture
def write_prereg() -> WritePrereg:
    """Write `<root>/PREREGISTRATION.md` pinning every file under data/attack_cases/, the golden
    file and the policy, plus the sampling values of providers.yaml. Keyword arguments change it:
    `extra_pins` adds or overrides a pin, `drop` removes pins, `text_after` is appended."""
    import hashlib

    import yaml

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def write(root: Path, *, extra_pins: Pins | None = None, drop: tuple[str, ...] = (),
              text_after: str = "") -> Path:
        files = [p for p in (root / "data" / "attack_cases").rglob("*") if p.is_file()]
        pins = {p.relative_to(root).as_posix(): digest(p) for p in files}
        for rel in ("configs/golden/behaviour_v1.yaml", "configs/policies/banking_v1.yaml"):
            pins[rel] = digest(root / rel)
        pins.update(extra_pins or {})
        for rel in drop:
            del pins[rel]
        providers = yaml.safe_load((root / "configs/providers.yaml").read_text(encoding="utf-8"))
        sampling = {
            f"{name}/{model}": {"temperature": body["sampling"]["temperature"],
                                "reasoning_effort": body["sampling"]["reasoning_effort"]}
            for name, p in providers["providers"].items()
            for model, body in p["models"].items() if body.get("sampling")
        }
        text = (
            "# Preregistration (test fixture)\n\n```yaml pin-block\n"
            + yaml.safe_dump(pins, sort_keys=True)
            + "```\n\n```yaml prereg-values\n"
            + yaml.safe_dump({"sampling": sampling}, sort_keys=True)
            + "```\n" + text_after
        )
        path = root / "PREREGISTRATION.md"
        path.write_bytes(text.encode("utf-8"))
        return path

    return write


@pytest.fixture
def repo(tmp_path: Path, write_prereg: WritePrereg) -> Path:
    """A fixture repository on which group A passes."""
    import shutil

    root = tmp_path / "repo"
    for tree in FIXTURE_TREES:
        shutil.copytree(PROJECT / tree, root / tree)
    for file in FIXTURE_FILES:
        shutil.copyfile(PROJECT / file, root / file)
    write_prereg(root)
    return root
