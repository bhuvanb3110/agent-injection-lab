"""Parity check: AgentDojo's ground-truth solutions must score as solved through our run_case.

If this fails, our scoring has drifted from AgentDojo's and no utility number can be trusted.
"""

from importlib.metadata import version

import pytest
from agentdojo.agent_pipeline.ground_truth_pipeline import GroundTruthPipeline

from lab.domains.agentdojo_bridge import Outcome, banking_task_ids, load_banking_suite, run_case


def test_agentdojo_version_is_pinned() -> None:
    assert version("agentdojo") == "0.1.35"


def test_banking_has_16_user_tasks() -> None:
    assert banking_task_ids() == [f"user_task_{n}" for n in range(16)]


@pytest.mark.parametrize("task_id", banking_task_ids())
def test_ground_truth_solves_banking_task(task_id: str) -> None:
    suite = load_banking_suite()
    task = suite.get_user_task_by_id(task_id)

    result = run_case(suite, GroundTruthPipeline(task), task)

    assert result.outcome is Outcome.SOLVED
    assert result.utility
    assert result.state_check_passed
