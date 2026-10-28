"""The policy engine: deterministic checks at the action checkpoint (T4, PLAN_M2 Q3).

The rules are data, in configs/policies/banking_v1.yaml (policy_v1). This module only applies
them. There is no handoff check: rules have nothing reliable to check in free text (PLAN_M2
Q3), so the policy sits at the action checkpoint only.

The policy makes no model call and writes no free text: its reasons are the fixed messages in
the rules file, which never quote a call's arguments, so a block error can't carry a marker.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from agentdojo.functions_runtime import TaskEnvironment
from pydantic import BaseModel, ConfigDict

from lab.defenses.gates import Checkpoint, HandoffCheck, ToolCallCheck, Verdict
from lab.models.types import RunContext

POLICY_ROLE = "policy"  # its agent ID and role in the trace
POLICY_V1_PATH = Path(__file__).resolve().parents[2] / "configs" / "policies" / "banking_v1.yaml"
ALLOWED = "allowed by every rule"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecipientRule(_Strict):
    tools: list[str]
    argument: str
    iban_pattern: str
    message: str


class RequestRule(_Strict):
    """The tool may run only if the user's request contains one of the keywords."""

    tool: str
    keywords: list[str]
    message: str


class Policy(_Strict):
    name: str
    suite: str
    recipients: RecipientRule
    password: RequestRule
    user_info: RequestRule

    def tools(self) -> set[str]:
        return {*self.recipients.tools, self.password.tool, self.user_info.tool}


def load_policy(path: Path = POLICY_V1_PATH) -> Policy:
    return Policy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def normalise_account(value: str) -> str:
    """IBANs are compared in upper case with whitespace removed."""
    return "".join(value.upper().split())


def trusted_recipients(policy: Policy, user_request: str, env: TaskEnvironment) -> frozenset[str]:
    """The IBANs in the user's request, plus the `recipient` and `sender` fields of the account's
    transactions and scheduled transactions, as they are when the run starts. Free text (subjects,
    files, tool outputs) is never read."""
    found = re.findall(policy.recipients.iban_pattern, user_request)
    account = env.bank_account  # type: ignore[attr-defined]  # Banking's environment
    for t in [*account.transactions, *account.scheduled_transactions]:
        found += [t.recipient, t.sender]
    return frozenset(normalise_account(v) for v in found)


class PolicyGate:
    def __init__(self, policy: Policy, checkpoints: frozenset[Checkpoint]) -> None:
        if checkpoints != {"action"}:
            raise ValueError(f"{policy.name} has rules for the action checkpoint only")
        self.policy = policy
        self.name = policy.name
        self.label = "policy"
        self.checkpoints = checkpoints
        self._request: str | None = None
        self._trusted: frozenset[str] = frozenset()

    def start(self, user_request: str, env: TaskEnvironment) -> None:
        self._request = user_request
        self._trusted = trusted_recipients(self.policy, user_request, env)

    def on_handoff(self, check: HandoffCheck, ctx: RunContext) -> Verdict:
        raise NotImplementedError(f"{self.name} has no handoff rules")

    def on_tool_call(self, check: ToolCallCheck, ctx: RunContext) -> Verdict:
        if self._request is None:
            raise RuntimeError(f"{self.name} was asked about a call before the run started")
        reason = self._violation(check.call.function, dict(check.call.args), self._request)
        return Verdict(
            approve=reason is None, reason=reason or ALLOWED,
            agent_id=POLICY_ROLE, agent_role=POLICY_ROLE,
        )

    def _violation(self, function: str, args: dict[str, object], request: str) -> str | None:
        """The message of the first rule the call breaks, or None."""
        rule = self.policy.recipients
        if function in rule.tools:
            recipient = args.get(rule.argument)
            # No recipient argument (an update of the amount or date only) sends nothing new.
            if recipient is not None and normalise_account(str(recipient)) not in self._trusted:
                return rule.message
        for request_rule in (self.policy.password, self.policy.user_info):
            if function == request_rule.tool and not any(
                k.casefold() in request.casefold() for k in request_rule.keywords
            ):
                return request_rule.message
        return None
