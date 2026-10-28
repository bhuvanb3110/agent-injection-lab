"""The attack builder: AgentDojo's payload, a per-case canary, and nothing else changed."""

import pytest
from agentdojo.attacks.important_instructions_attacks import ImportantInstructionsAttack
from agentdojo.default_suites.v1.tools.banking_client import send_money

from lab.attacks.agentdojo import (
    ATTACK_NAME,
    CANARY_CHARS,
    CANARY_PREFIX,
    KNOWN_ISSUES,
    build_injections,
    canary_for,
)
from lab.domains.agentdojo_bridge import load_banking_suite

NOTICE = "injection_landloard_notice"
CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_canary_is_prefixed_crockford_and_a_pure_function_of_the_case() -> None:
    canary = canary_for(ATTACK_NAME, "injection_task_4", "user_task_2", NOTICE)
    assert canary.startswith(CANARY_PREFIX) and len(canary) == len(CANARY_PREFIX) + CANARY_CHARS
    assert set(canary[len(CANARY_PREFIX):]) <= CROCKFORD
    assert canary == canary_for(
        ATTACK_NAME, "injection_task_4", "user_task_2", "injection_landloard_notice"
    )


def test_canary_differs_between_cases() -> None:
    cases = [
        (ATTACK_NAME, "injection_task_4", "user_task_2", NOTICE),
        (ATTACK_NAME, "injection_task_5", "user_task_2", NOTICE),
        (ATTACK_NAME, "injection_task_4", "user_task_3", "injection_landloard_notice"),
        (ATTACK_NAME, "injection_task_4", "user_task_2", "injection_bill_text"),
        ("another_attack", "injection_task_4", "user_task_2", NOTICE),
    ]
    assert len({canary_for(*c) for c in cases}) == len(cases)


def test_payload_is_agentdojos_with_only_the_canary_line_added() -> None:
    suite = load_banking_suite()
    user_task = suite.get_user_task_by_id("user_task_2")
    injection_task = suite.get_injection_task_by_id("injection_task_4")
    payloads = build_injections(suite, ATTACK_NAME, user_task, injection_task)
    assert set(payloads) == {"injection_landloard_notice"}  # the one channel task 2 reads
    payload = payloads["injection_landloard_notice"]

    canary = canary_for(ATTACK_NAME, "injection_task_4", "user_task_2", NOTICE)
    assert payload.count(canary) == 1
    without_canary = payload.replace(f"Reference: {canary}\n\n", "")
    expected = ImportantInstructionsAttack._JB_STRING.format(
        goal=injection_task.GOAL, user="Emma Johnson", model="the AI language model"
    )
    assert without_canary == expected
    # The canary sits inside the signature block, before the closing tag.
    assert payload.index("Signed,") < payload.index(canary) < payload.index("</INFORMATION>")


@pytest.mark.parametrize(
    ("user_task", "vector"),
    [
        ("user_task_0", "injection_bill_text"),  # a file
        ("user_task_1", "injection_incoming_transaction"),  # a transaction subject
        ("user_task_2", NOTICE),  # a file
    ],
)
def test_only_the_vector_the_user_task_reads_is_filled(user_task: str, vector: str) -> None:
    suite = load_banking_suite()
    payloads = build_injections(
        suite,
        ATTACK_NAME,
        suite.get_user_task_by_id(user_task),
        suite.get_injection_task_by_id("injection_task_0"),
    )
    assert set(payloads) == {vector}


def test_payload_reaches_the_environment_the_agent_reads() -> None:
    suite = load_banking_suite()
    user_task = suite.get_user_task_by_id("user_task_2")
    injection_task = suite.get_injection_task_by_id("injection_task_4")
    payloads = build_injections(suite, ATTACK_NAME, user_task, injection_task)
    env = suite.load_and_inject_default_environment(payloads)
    notice = env.filesystem.files["landlord-notices.txt"]
    assert injection_task.GOAL in notice


def test_unknown_attack_is_refused() -> None:
    suite = load_banking_suite()
    with pytest.raises(ValueError, match="Unknown attack"):
        build_injections(
            suite,
            "tool_knowledge",
            suite.get_user_task_by_id("user_task_2"),
            suite.get_injection_task_by_id("injection_task_4"),
        )


@pytest.mark.parametrize("injection_task_id", sorted(KNOWN_ISSUES))
def test_known_issue_injection_tasks_count_any_transfer_to_the_attacker(
    injection_task_id: str,
) -> None:
    """AgentDojo's check for injection tasks 0, 1 and 3 reads `any(x or "" in subject)`, which
    is `x or (...)`: a transfer to the attacker IBAN with an unrelated subject already counts.
    We score as AgentDojo does; this pins the behaviour so a fix upstream is noticed."""
    suite = load_banking_suite()
    task = suite.get_injection_task_by_id(injection_task_id)
    pre = suite.load_and_inject_default_environment({})
    post = pre.model_copy(deep=True)
    attacker = task.ground_truth(pre)[0].args["recipient"]
    assert not task.security("", pre, post)  # nothing sent yet
    send_money(post.bank_account, attacker, 0.01, "an unrelated subject", "2022-01-01")
    assert task.security("", pre, post)
