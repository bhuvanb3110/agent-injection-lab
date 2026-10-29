"""What `lab run` refuses, and what `lab report` and `lab trace` may show (D102, D103, D117).

**The guard.** `check_run` is called by `lab run` before anything is built. An experiment is
*registered* if it contains a test-split case, or if it has no attack case at all and its config
is pinned in the prereg (the benign controls of M4). A registered experiment runs only if
`validate-rules` group A passes, and its runs carry the prereg's SHA-256 (`prereg_sha256`).
Anything else, every dev-case experiment included, passes through untouched, with no
`prereg_sha256`: its run IDs, config hash and scoring are what they were.

An absent PREREGISTRATION.md fails group A, so test cases stay refused until step 11 writes it.

**The blinding.** A run is *test-screen* if its case is one of the 50 (`<domain>_test_screen.json`);
a run of milestone 2's `important_instructions` attack is one of the 144 AgentDojo cases, found
by its pair. While the freeze log has no valid unblinding record (`prereg.blinding_state`), `lab
report` and `lab trace` do not show the outcomes of those runs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from lab.config import ExperimentConfig
from lab.errors import ConfigError
from lab.rules import PROJECT_ROOT
from lab.rules.checks import Check, run_group_a
from lab.rules.prereg import PREREG_NAME, PreregError, load_pins, prereg_sha256
from lab.rules.scope import case_id_of, load_split_data, scope_of
from lab.scheduler.spec import RunSpec

GroupA = Callable[[Path], list[Check]]
MAX_PROBLEMS_SHOWN = 5


def is_pinned(config_path: Path, root: Path) -> bool:
    """Whether the prereg's pin block names this config file."""
    try:
        relative = config_path.resolve().relative_to(root.resolve()).as_posix()
        return relative in load_pins(root)
    except (ValueError, OSError, PreregError):
        return False


def check_run(
    config: ExperimentConfig,
    config_path: Path,
    root: Path = PROJECT_ROOT,
    group_a: GroupA | None = None,
) -> str | None:
    """Raise `ConfigError` if `lab run` must refuse this experiment; otherwise return the
    prereg hash to stamp on its runs, or None if it is not a registered experiment."""
    try:
        scope = scope_of(config, load_split_data(root))
    except (OSError, ValueError, KeyError) as error:
        raise ConfigError(
            f"cannot tell which split {config.name!r} would run, so it is refused: {error}"
        ) from error
    if not (scope.has_test or (not scope.has_attack_cases and is_pinned(config_path, root))):
        return None
    why = "contains test-split cases" if scope.has_test else "is pinned in the prereg"
    if prereg_sha256(root) is None:
        raise ConfigError(
            f"{config.name!r} {why}, and {PREREG_NAME} does not exist, so `validate-rules` "
            "group A fails. Test cases stay refused until it is written and committed."
        )
    failed = [c for c in (group_a or run_group_a)(root) if not c.ok]
    if failed:
        shown = [f"{c.name}: {c.problems[0]}" for c in failed[:MAX_PROBLEMS_SHOWN]]
        raise ConfigError(
            f"{config.name!r} {why}, and `validate-rules` group A fails "
            f"({len(failed)} check(s)): " + "; ".join(shown)
            + ". Run `lab validate-rules` for the full list."
        )
    return prereg_sha256(root)


def is_test_screen(spec: RunSpec, test_screen: frozenset[str]) -> bool:
    case = case_id_of(spec.case_id, spec.user_task_id, spec.injection_task_id)
    return case is not None and case in test_screen
