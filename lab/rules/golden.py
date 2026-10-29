"""The golden behaviour fixtures (PLAN_M3 §5 group A item 7, D102, D117).

Rules that live in code are pinned by what they do, not by their source (D102), so an unrelated
refactor raises no alarm and a change of behaviour does. The fixtures are data in
`configs/golden/behaviour_v1.yaml`, which the prereg pins by hash: weakening a fixture to match
a changed rule is then a change to a pinned file.

Covered, each by a table of (input, frozen answer):
- `normalise` (D77);
- `ended_with_question` and `requested_user_input` (D73, D74);
- `attacker_effect` (D86);
- `summary_parser_v1` (D65), also by the digest of the parser file;
- the sabotage constant and `lab_sabotage_0` (D96, D109).

The same file holds the numbers group B re-checks against the run store and the label files
(`rechecks`, D89): D74's confusion table and D88's agreement.

An environment in a fixture is the Banking default environment plus a few operations, so a
fixture is a few lines and not a dump. In an operation, `$attacker` and `$attacker_lowercase`
stand for the attacker IBAN and `$sabotage` for the planted amount, so none of them is spelled
out twice.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

GOLDEN_PATH = "configs/golden/behaviour_v1.yaml"


class GoldenError(ValueError):
    """The golden file is missing or malformed."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NormaliseCase(_Strict):
    input: str
    expected: str


class FinalAnswerCase(_Strict):
    """A run's final message and what the two "asked the user" rules say about it."""

    text: str | None
    outcome: str = "not_solved"
    tool_calls: bool = False  # the last message also calls a tool
    ended_with_question: bool
    requested_user_input: bool


class EnvOp(_Strict):
    op: Literal["add_transaction", "add_scheduled", "edit_scheduled"]
    id: int | None = None  # edit_scheduled: which scheduled transaction
    recipient: str | None = None
    amount: float | Literal["$sabotage"] | None = None
    subject: str | None = None


class EnvCase(_Strict):
    name: str
    pre_ops: list[EnvOp] = []  # applied to the starting environment, which `post` copies
    ops: list[EnvOp]  # applied to `post` only
    expected: bool


class SummaryCase(_Strict):
    user_task: str
    summary: str | None
    step: str
    approve: bool


class SabotageSection(_Strict):
    amount: float
    cases: list[EnvCase]


class D74Recheck(_Strict):
    experiments: list[str]  # whose annotated runs the table is computed over
    # rule name -> [true positives, false positives, false negatives, true negatives]
    tables: dict[str, list[int]]


class D88Recheck(_Strict):
    file: str
    n: int
    agree: int


class Rechecks(_Strict):
    d74: D74Recheck
    d88: D88Recheck


class Golden(_Strict):
    version: int
    normalise: list[NormaliseCase]
    final_answers: list[FinalAnswerCase]
    attacker_effect: list[EnvCase]
    summary_parser_digest: str
    summary_parser: list[SummaryCase]
    sabotage: SabotageSection
    rechecks: Rechecks


def load_golden(root: Path) -> Golden:
    path = root / GOLDEN_PATH
    if not path.is_file():
        raise GoldenError(f"{GOLDEN_PATH} does not exist")
    try:
        return Golden.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValidationError) as error:
        raise GoldenError(f"{GOLDEN_PATH} is malformed: {error}") from error


# --- environments --------------------------------------------------------------------------


def _environment(ops: list[EnvOp], base: Any) -> Any:
    """`base` (a Banking environment) changed in place by `ops`, and returned."""
    from agentdojo.default_suites.v1.tools.banking_client import Transaction

    from lab.attacks.agentdojo import ATTACKER_IBAN
    from lab.attacks.predicates import SABOTAGE_AMOUNT

    symbols: dict[str, Any] = {
        "$attacker": ATTACKER_IBAN,
        "$attacker_lowercase": ATTACKER_IBAN.lower(),
        "$sabotage": SABOTAGE_AMOUNT,
    }

    def value(raw: Any) -> Any:
        return symbols[raw] if isinstance(raw, str) and raw in symbols else raw

    account = base.bank_account
    for op in ops:
        if op.op == "edit_scheduled":
            changes = {
                k: value(v) for k, v in (("recipient", op.recipient), ("amount", op.amount),
                                         ("subject", op.subject)) if v is not None
            }
            positions = [i for i, t in enumerate(account.scheduled_transactions) if t.id == op.id]
            if len(positions) != 1:
                raise GoldenError(f"edit_scheduled: no scheduled transaction with id {op.id}")
            old = account.scheduled_transactions[positions[0]]
            account.scheduled_transactions[positions[0]] = old.model_copy(update=changes)
            continue
        target = (account.transactions if op.op == "add_transaction"
                  else account.scheduled_transactions)
        target.append(Transaction(
            id=1000 + len(target), sender=account.iban, recipient=value(op.recipient) or "",
            amount=value(op.amount) or 0.0, subject=op.subject or "", date="2022-04-01",
            recurring=op.op == "add_scheduled",
        ))
    return base


def _pre_and_post(case: EnvCase) -> tuple[Any, Any]:
    from lab.domains.agentdojo_bridge import load_banking_suite

    suite = load_banking_suite()
    pre = _environment(case.pre_ops, suite.load_and_inject_default_environment({}))
    return pre, _environment(case.ops, pre.model_copy(deep=True))


# --- the checks ----------------------------------------------------------------------------


def _record(case: FinalAnswerCase) -> Any:
    from lab.scheduler.spec import RunSpec
    from lab.store import RunRecord

    spec = RunSpec(
        experiment="golden", suite="banking", benchmark_version="v1.2.2",
        user_task_id="user_task_0", topology="t1", provider="fake", model_id="golden",
        temperature=0.0, reasoning_effort=None, max_tokens=None, seed=None, repeat_idx=0,
    )
    content = None if case.text is None else [{"type": "text", "content": case.text}]
    message = {"role": "assistant", "content": content,
               "tool_calls": [{"function": "get_iban", "args": {}}] if case.tool_calls else None}
    return RunRecord(
        run_id=spec.run_id, spec=spec, config_hash="golden", git_commit="golden",
        agentdojo_version="golden", seed=None, outcome=case.outcome, outcome_subtype=None,
        utility=case.outcome == "solved", state_check_passed=None, error=None, calls=[],
        malformed_tool_calls=0, messages=[message], started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:00+00:00", wall_latency_s=0.0,
    )


def _normalise(golden: Golden) -> list[str]:
    from lab.tracing.markers import normalise

    return [f"normalise({c.input!r}) gave {got!r}, frozen {c.expected!r}"
            for c in golden.normalise if (got := normalise(c.input)) != c.expected]


def _final_answers(golden: Golden) -> list[str]:
    from lab.scoring.attack_metrics import ended_with_question, requested_user_input

    problems = []
    for c in golden.final_answers:
        record = _record(c)
        for rule, got, frozen in (
            ("ended_with_question", ended_with_question(record), c.ended_with_question),
            ("requested_user_input", requested_user_input(record), c.requested_user_input),
        ):
            if got != frozen:
                problems.append(f"{rule}({c.text!r}, {c.outcome}) gave {got}, frozen {frozen}")
    return problems


def _attacker_effect(golden: Golden) -> list[str]:
    from lab.scoring.attacker_effect import attacker_effect

    problems = []
    for c in golden.attacker_effect:
        pre, post = _pre_and_post(c)
        if (got := attacker_effect(pre, post)) != c.expected:
            problems.append(f"attacker_effect[{c.name}] gave {got}, frozen {c.expected}")
    return problems


def _summary_parser(golden: Golden) -> list[str]:
    import hashlib

    from lab.defenses.human import intent_spec, load_summary_parser, read_summary
    from lab.domains.agentdojo_bridge import load_banking_suite

    parser = load_summary_parser()
    problems = []
    digest = hashlib.sha256(parser.model_dump_json().encode()).hexdigest()
    if digest != golden.summary_parser_digest:
        problems.append(f"summary_parser_v1 digest is {digest}, frozen "
                        f"{golden.summary_parser_digest}")
    suite = load_banking_suite()
    for c in golden.summary_parser:
        reading = read_summary(parser, c.summary, intent_spec(suite, c.user_task))
        if (reading.step, reading.approve) != (c.step, c.approve):
            problems.append(f"summary_parser_v1[{c.user_task}, {c.summary!r}] gave "
                            f"({reading.step}, {reading.approve}), frozen ({c.step}, {c.approve})")
    return problems


def _sabotage(golden: Golden) -> list[str]:
    from lab.attacks.predicates import SABOTAGE_AMOUNT, lab_predicate
    from lab.domains.agentdojo_bridge import load_banking_suite

    problems = []
    if golden.sabotage.amount != SABOTAGE_AMOUNT:
        problems.append(f"SABOTAGE_AMOUNT is {SABOTAGE_AMOUNT}, frozen {golden.sabotage.amount}")
    env = load_banking_suite().load_and_inject_default_environment({})
    account = env.bank_account
    amounts = [round(t.amount, 2) for t in (*account.transactions, *account.scheduled_transactions)]
    if SABOTAGE_AMOUNT in (*amounts, round(account.balance, 2)):
        problems.append("the planted amount is in the starting environment")
    predicate = lab_predicate("lab_sabotage_0")
    for c in golden.sabotage.cases:
        pre, post = _pre_and_post(c)
        if (got := predicate(pre, post, "")) != c.expected:
            problems.append(f"lab_sabotage_0[{c.name}] gave {got}, frozen {c.expected}")
    return problems


SECTIONS: dict[str, Callable[[Golden], list[str]]] = {
    "normalise": _normalise,
    "ended_with_question and requested_user_input": _final_answers,
    "attacker_effect": _attacker_effect,
    "summary_parser_v1": _summary_parser,
    "sabotage": _sabotage,
}


def golden_problems(root: Path) -> list[str]:
    """Every fixture the code no longer answers as frozen, or why the file cannot be read."""
    try:
        golden = load_golden(root)
    except GoldenError as error:
        return [str(error)]
    problems = []
    for name, run in SECTIONS.items():
        try:
            problems += [f"{name}: {p}" for p in run(golden)]
        except Exception as error:  # a rule that raises has changed behaviour too
            problems.append(f"{name}: raised {type(error).__name__}: {error}")
    return problems
