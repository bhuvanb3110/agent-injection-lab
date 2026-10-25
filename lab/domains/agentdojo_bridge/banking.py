"""AgentDojo Banking suite, pinned to one benchmark version (DECISIONS.md D3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentdojo.task_suite.load_suites import get_suite

if TYPE_CHECKING:
    # Type-only import: importing the banking module before `load_suites` triggers a
    # circular import inside agentdojo 0.1.35.
    from agentdojo.default_suites.v1.banking.task_suite import BankingEnvironment
    from agentdojo.task_suite.task_suite import TaskSuite

BENCHMARK_VERSION = "v1.2.2"
BANKING_SUITE_NAME = "banking"

# Banking v1.2.2 user tasks whose utility check passes for an agent that does nothing (D37):
# task 8 always passes, tasks 9 and 10 pass on an unchanged environment, and the seed
# environment already satisfies tasks 5 and 6. The other 11 tasks are the "discriminating" ones
# the headline utility is computed on (D39). tests/test_do_nothing_floor.py re-derives this set
# from AgentDojo's own predicates, so it can't drift from the installed tasks.
DO_NOTHING_PASSES = frozenset(
    {"user_task_5", "user_task_6", "user_task_8", "user_task_9", "user_task_10"}
)


def load_banking_suite() -> TaskSuite[BankingEnvironment]:
    """Return the Banking suite at the pinned benchmark version.

    `get_suite` (agentdojo/task_suite/load_suites.py) picks, for each task, the newest
    definition at or below the requested version.
    """
    suite: TaskSuite[BankingEnvironment] = get_suite(BENCHMARK_VERSION, BANKING_SUITE_NAME)
    return suite


def banking_task_ids() -> list[str]:
    """User task IDs in numeric order: user_task_0, user_task_1, ..., user_task_15."""
    return sorted(
        load_banking_suite().user_tasks, key=lambda task_id: int(task_id.rsplit("_", 1)[1])
    )
