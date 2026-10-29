"""AttackCase schema, loader and derived fields (PLAN_M3 step 2; D93, D94). No model, no network."""

from collections import Counter
from itertools import product
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lab.attacks.agentdojo import ImportantInstructionsWithCanary
from lab.attacks.cases import (
    AGENTDOJO_GOALS,
    WRITE_TOOLS,
    AttackCase,
    agentdojo_cases,
    ground_truth_write_tools,
    load_cases,
    write_worker,
)
from lab.config import ExperimentConfig, expand, load_experiment
from lab.domains.agentdojo_bridge import (
    banking_injection_task_ids,
    banking_task_ids,
    load_banking_suite,
)
from lab.models.providers import load_providers
from lab.topologies.t2 import WORKER_TOOLS

EXPERIMENTS = Path(__file__).resolve().parents[1] / "configs" / "experiments"

TX = "injection_incoming_transaction"
ADDRESS = "injection_address_change"
BILL = "injection_bill_text"


def row(
    vectors: list[str], injection_task: str = "injection_task_4", **extra: Any
) -> dict[str, Any]:
    """A hand-made case row without derived fields."""
    return {
        "id": "hand:" + injection_task + ":" + "+".join(vectors),
        "suite": "banking",
        "user_task_id": "user_task_3",
        "injection_task_id": injection_task,
        "technique": "plain_instruction",
        "goal": "unauthorized_action",
        "check": f"agentdojo:{injection_task}.security",
        "vectors": vectors,
    } | extra


def case(vectors: list[str], injection_task: str = "injection_task_4", **extra: Any) -> AttackCase:
    return AttackCase.model_validate(row(vectors, injection_task, **extra))


# --- the 144 AgentDojo pairs -------------------------------------------------------------------


def test_the_144_cases_equal_get_injection_candidates_for_every_pair() -> None:
    suite = load_banking_suite()
    attack = ImportantInstructionsWithCanary(suite)
    cases = agentdojo_cases()
    assert len(cases) == 144 == len(banking_task_ids()) * len(banking_injection_task_ids())
    assert {(c.user_task_id, c.injection_task_id) for c in cases} == set(
        product(banking_task_ids(), banking_injection_task_ids())
    )
    for c in cases:
        expected = attack.get_injection_candidates(suite.get_user_task_by_id(c.user_task_id))
        assert c.vectors == list(expected)
        assert c.technique == "important_instructions"


def test_the_144_split_into_108_tool_output_36_file_and_no_multi() -> None:
    counts = Counter(c.channel for c in agentdojo_cases())
    assert counts == {"tool_output": 108, "file": 36}
    assert all(len(c.vectors) == 1 for c in agentdojo_cases())


def test_ids_are_unique_and_duplicates_are_refused() -> None:
    cases = agentdojo_cases()
    assert len({c.id for c in cases}) == 144
    with pytest.raises(ValueError, match="Duplicate case IDs"):
        load_cases([row([TX]), row([TX])])


def test_agentdojo_goals_cover_all_nine_injection_tasks() -> None:
    assert sorted(AGENTDOJO_GOALS) == banking_injection_task_ids()
    assert Counter(AGENTDOJO_GOALS.values()) == {"exfiltration": 5, "unauthorized_action": 4}


def test_every_check_is_the_injection_tasks_own() -> None:
    for c in agentdojo_cases():
        assert c.check == f"agentdojo:{c.injection_task_id}.security"
    with pytest.raises(ValidationError, match="not its injection task's own"):
        case([TX], check="agentdojo:injection_task_5.security")


# --- the schema ---------------------------------------------------------------------------------


def test_round_trip() -> None:
    for c in [*agentdojo_cases(), case([TX, ADDRESS], technique="multi_hop")]:
        assert AttackCase.model_validate(c.model_dump(mode="json")) == c
        assert AttackCase.model_validate_json(c.model_dump_json()) == c
    rows = [c.model_dump(mode="json") for c in agentdojo_cases()]
    assert load_cases(rows) == agentdojo_cases()


def test_a_stored_derived_value_must_equal_the_computed_one() -> None:
    good = case([TX])
    for name, wrong in [
        ("channel", "file"),
        ("t2_stratum", "cross_worker"),
        ("required_hops", ["account"]),
    ]:
        stored = good.model_dump(mode="json") | {name: wrong}
        with pytest.raises(ValidationError, match=f"{name} is derived"):
            AttackCase.model_validate(stored)


def test_unknown_fields_vectors_and_tasks_are_refused() -> None:
    with pytest.raises(ValidationError):
        case([TX], typo=1)
    with pytest.raises(ValidationError, match="Unknown vector"):
        case(["injection_nope"])
    with pytest.raises(ValidationError, match="at least one vector"):
        case([])
    with pytest.raises(ValueError, match="unknown user or injection task"):
        load_cases([row([TX], user_task_id="user_task_99")])


# --- the channel rule (D94) ---------------------------------------------------------------------


def test_channel_of_a_single_vector_case() -> None:
    assert case([TX]).channel == "tool_output"
    assert case([BILL]).channel == "file"
    assert case(["tool_description:read_file"]).channel == "tool_description"


def test_channel_of_a_same_channel_multi_vector_case_is_that_channel() -> None:
    assert case([BILL, ADDRESS]).channel == "file"
    assert case([BILL, "injection_landloard_notice", ADDRESS]).channel == "file"


def test_channel_of_a_cross_channel_case_is_multi() -> None:
    assert case([TX, ADDRESS]).channel == "multi"
    assert case([TX, "tool_description:get_most_recent_transactions"]).channel == "multi"
    assert case(["tool_description:read_file", BILL]).channel == "multi"


def test_tool_description_is_built_for_two_read_tools_only() -> None:
    with pytest.raises(ValidationError, match="No tool_description vector"):
        case(["tool_description:send_money"])


# --- t2_stratum and required_hops ---------------------------------------------------------------


def test_stratum_local_when_the_vector_worker_holds_the_write() -> None:
    c = case([TX], "injection_task_4")  # payments worker both reads and writes
    assert (c.t2_stratum, c.required_hops) == ("local", ["payments"])
    c = case([BILL], "injection_task_7")  # read_file and update_password are both account
    assert (c.t2_stratum, c.required_hops) == ("local", ["account"])


def test_stratum_cross_worker_when_they_differ() -> None:
    c = case([TX], "injection_task_7")  # payments worker reads it, account worker writes
    assert (c.t2_stratum, c.required_hops) == ("cross_worker", ["payments", "planner", "account"])
    c = case([BILL], "injection_task_4")
    assert (c.t2_stratum, c.required_hops) == ("cross_worker", ["account", "planner", "payments"])


def test_tool_description_follows_the_tool_it_describes() -> None:
    assert case(["tool_description:get_most_recent_transactions"]).t2_stratum == "local"
    assert case(["tool_description:read_file"]).t2_stratum == "cross_worker"


def test_two_vector_multi_hop_is_cross_worker_with_the_full_path() -> None:
    to_payment = case([TX, ADDRESS], "injection_task_4", technique="multi_hop")
    assert to_payment.channel == "multi"
    assert to_payment.t2_stratum == "cross_worker"
    assert to_payment.required_hops == ["payments", "planner", "account", "planner", "payments"]
    to_account = case([TX, ADDRESS], "injection_task_7", technique="multi_hop")
    assert to_account.t2_stratum == "cross_worker"
    assert to_account.required_hops == ["payments", "planner", "account"]


def test_a_multi_hop_case_that_stays_in_one_worker_is_refused() -> None:
    with pytest.raises(ValidationError, match="must be cross_worker"):
        case([TX, "tool_description:get_most_recent_transactions"], technique="multi_hop")


# --- the code facts the strata rest on ----------------------------------------------------------


def test_write_tools_and_worker_tools_split_the_banking_tools_exactly() -> None:
    all_tools = {t for tools in WORKER_TOOLS.values() for t in tools}
    assert all_tools >= WRITE_TOOLS
    assert all(t.startswith("get_") or t == "read_file" for t in all_tools - WRITE_TOOLS)


@pytest.mark.parametrize("injection_task_id", banking_injection_task_ids())
def test_every_injection_task_has_write_tools_in_exactly_one_worker(
    injection_task_id: str,
) -> None:
    # If this fails, local vs cross_worker is undefined for that task: stop and ask (PLAN_M3).
    assert ground_truth_write_tools(injection_task_id)
    assert write_worker(injection_task_id) in WORKER_TOOLS


def test_the_write_workers_of_the_nine_tasks() -> None:
    assert {i: write_worker(i) for i in banking_injection_task_ids()} == {
        **{f"injection_task_{i}": "payments" for i in (0, 1, 2, 3, 4, 5, 6, 8)},
        "injection_task_7": "account",
    }


def test_strata_of_the_144_follow_from_the_vector_and_the_write_worker() -> None:
    counts = Counter((c.channel, c.t2_stratum) for c in agentdojo_cases())
    # tool_output: 12 tasks x 9 (payments reads; only injection_task_7 writes in account).
    # file: 4 tasks x 9 (account reads; only injection_task_7 writes in account).
    assert counts == {
        ("tool_output", "local"): 12 * 8,
        ("tool_output", "cross_worker"): 12,
        ("file", "local"): 4,
        ("file", "cross_worker"): 4 * 8,
    }


# --- configs ------------------------------------------------------------------------------------


def test_cases_field_at_its_default_leaves_hash_and_run_ids_unchanged() -> None:
    config = load_experiment(EXPERIMENTS / "smoke_topologies.yaml")
    explicit = ExperimentConfig.model_validate(config.model_dump(mode="json") | {"cases": None})
    assert explicit.config_hash == config.config_hash
    assert [s.run_id for s in expand(explicit, load_providers())] == [
        s.run_id for s in expand(config, load_providers())
    ]


def test_naming_a_case_set_changes_the_hash_and_cannot_be_expanded_yet() -> None:
    config = load_experiment(EXPERIMENTS / "smoke.yaml")
    named = ExperimentConfig.model_validate(
        config.model_dump(mode="json") | {"cases": "banking_v1"}
    )
    assert named.config_hash != config.config_hash
    with pytest.raises(ValueError, match="not built"):
        expand(named, load_providers())
