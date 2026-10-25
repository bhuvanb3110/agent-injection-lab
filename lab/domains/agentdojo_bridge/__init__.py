"""Bridge to AgentDojo suites: load them at a pinned version and run single cases."""

from lab.domains.agentdojo_bridge.banking import (
    BANKING_SUITE_NAME,
    BENCHMARK_VERSION,
    banking_task_ids,
    load_banking_suite,
)
from lab.domains.agentdojo_bridge.run_case import CaseResult, Outcome, run_case

__all__ = [
    "BANKING_SUITE_NAME",
    "BENCHMARK_VERSION",
    "CaseResult",
    "Outcome",
    "banking_task_ids",
    "load_banking_suite",
    "run_case",
]
