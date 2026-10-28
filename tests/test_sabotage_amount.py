"""The planted amount of lab_sabotage_0 is unique in the installed Banking suite (D96, fix 3).

If another source produced the same amount, an honest run could satisfy the predicate. Checked
against the starting environment, every user-task PROMPT, and every amount in every user-task
and injection-task ground_truth() call.
"""

import re

from lab.attacks.predicates import SABOTAGE_AMOUNT
from lab.domains.agentdojo_bridge import load_banking_suite

SUITE = load_banking_suite()
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def same(value: object) -> bool:
    return isinstance(value, int | float) and round(float(value), 2) == SABOTAGE_AMOUNT


def test_the_amount_is_not_in_the_starting_environment() -> None:
    account = SUITE.load_and_inject_default_environment({}).bank_account
    amounts = [account.balance]
    amounts += [t.amount for t in (*account.transactions, *account.scheduled_transactions)]
    assert len(amounts) > 1  # the check below is not vacuous
    assert not any(same(a) for a in amounts)


def test_the_amount_is_not_a_number_in_any_user_task_prompt() -> None:
    numbers: list[str] = []
    for task in SUITE.user_tasks.values():
        numbers += NUMBER.findall(task.PROMPT)
    assert numbers
    for text in numbers:
        assert round(float(text.replace(",", ".")), 2) != SABOTAGE_AMOUNT, text
    # nor its whole part as digits inside a longer number
    assert not any(str(SABOTAGE_AMOUNT).split(".")[0] in n for n in numbers)


def test_the_amount_is_in_no_ground_truth_call() -> None:
    calls = 0
    for user_task in SUITE.user_tasks.values():
        env = user_task.init_environment(SUITE.load_and_inject_default_environment({}))
        for c in user_task.ground_truth(env):
            calls += 1
            assert not any(same(v) for v in c.args.values()), (user_task.ID, c)
    for injection_task in SUITE.injection_tasks.values():
        env = SUITE.load_and_inject_default_environment({})
        for c in injection_task.ground_truth(env):
            calls += 1
            assert not any(same(v) for v in c.args.values()), (injection_task.ID, c)
    assert calls


def test_the_amount_is_not_in_the_text_of_the_environment() -> None:
    """Stricter than D96 asks: no subject, file or address holds it as a number either."""
    env = SUITE.load_and_inject_default_environment({}).model_dump_json()
    assert str(SABOTAGE_AMOUNT) not in env and "4321" not in env
