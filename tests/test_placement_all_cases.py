"""Every case of the dataset can be put into a real AgentDojo environment (D115, PLAN_M3 step 8).

AgentDojo writes an environment payload into a YAML text (`load_and_inject_default_environment`),
so a wording with a line starting `---` raised in the middle of a queue (D114, defect 1). This
test builds the payload of ALL 225 cases, dev and test, through the same path the runner uses
(`plan_case`, which calls `place()`), and writes it with the same calls. No model is called.

For each case:
- an environment vector (a transaction subject, a file) must load, and what lands in the
  environment must hold the payload: whitespace aside (AgentDojo's YAML folds a newline into a
  space for every payload, its own included), nothing is lost, and every canary and plain goal
  marker the trace looks for is findable there;
- a tool-description vector must end the tool's description with the payload, exactly, and the
  tool's schema must serialise;
- multi-hop's two vectors both land, each in its own place.
"""

from __future__ import annotations

import json
from typing import Any

from lab.attacks.case_plan import plan_case
from lab.attacks.case_set import committed_case_set
from lab.attacks.cases import TOOL_DESCRIPTION_PREFIX, AttackCase
from lab.domains.agentdojo_bridge import load_banking_suite
from lab.domains.agentdojo_bridge.run_case import with_injected_descriptions
from lab.models.llm_element import function_to_openai
from lab.tracing.markers import normalise

SUITE = load_banking_suite()
CASE_SET = committed_case_set("banking")


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def squash(text: str) -> str:
    return "".join(text.split())


def problems_with(case: AttackCase, split: str) -> list[str]:
    """Everything wrong with placing one case; empty if it can be placed."""
    plan = plan_case(SUITE, case, split)  # type: ignore[arg-type]
    found: list[str] = []
    landed = ""
    if plan.injections:
        try:
            environment = SUITE.load_and_inject_default_environment(plan.injections)
        except Exception as error:
            return [f"environment load: {type(error).__name__}: {str(error).splitlines()[0]}"]
        landed = squash("\x00".join(strings(json.loads(environment.model_dump_json()))))
        for vector, text in plan.injections.items():
            if squash(text) not in landed:
                found.append(f"{vector}: the payload is not in the loaded environment")
    if plan.tool_descriptions:
        try:
            tools = {f.name: f for f in with_injected_descriptions(
                SUITE.tools, plan.tool_descriptions)}
        except Exception as error:
            return [*found, f"tool description: {type(error).__name__}: {error}"]
        for name, injected in plan.tool_descriptions.items():
            description = tools[name].description
            if not description.endswith(f"\n\n{injected.text}"):
                found.append(f"{name}: the description does not end with the payload")
            try:
                json.dumps(function_to_openai(tools[name]))
            except Exception as error:
                found.append(f"{name}: the schema does not serialise: {error}")
            landed += squash(description)
    # The matcher normalises both sides; a marker it looks for in plain text must be findable.
    # (An obfuscated goal marker is declared instead, so only canaries are required of those.)
    for marker in plan.markers:
        encoded_goal = marker.kind == "goal" and case.technique == "obfuscation"
        in_text = normalise(marker.value) in normalise(landed)
        if not in_text and not encoded_goal:
            found.append(f"marker {marker.value!r} is not findable in what landed")
    expected = len(case.vectors)
    if len(plan.injections) + len(plan.tool_descriptions) != expected:
        found.append(f"{expected} vectors, {len(plan.injections)} + "
                     f"{len(plan.tool_descriptions)} placed")
    return found


def test_every_one_of_the_225_cases_can_be_placed_in_a_real_environment() -> None:
    assert len(CASE_SET.cases) == 225
    splits = {CASE_SET.split_of[i] for i in CASE_SET.cases}
    assert splits == {"dev", "test"}
    failures = {
        case.id: problems
        for case in CASE_SET.cases.values()
        if (problems := problems_with(case, CASE_SET.split_of[case.id]))
    }
    assert failures == {}


def test_the_cases_cover_every_vector_type() -> None:
    """The test above would be empty praise if a vector type never occurred in the dataset."""
    cases = list(CASE_SET.cases.values())
    kinds = {
        "tool result (transaction subject)": any(
            "injection_incoming_transaction" in c.vectors and c.technique != "multi_hop"
            for c in cases),
        "file": any(
            any(v in ("injection_bill_text", "injection_landloard_notice",
                      "injection_address_change") for v in c.vectors) and c.technique != "multi_hop"
            for c in cases),
        "tool description": any(
            any(v.startswith(TOOL_DESCRIPTION_PREFIX) for v in c.vectors) for c in cases),
        "multi-hop's two vectors": any(
            c.technique == "multi_hop" and len(c.vectors) == 2 for c in cases),
    }
    assert all(kinds.values()), kinds


def test_a_multi_hop_pointer_and_action_land_in_different_places() -> None:
    case = next(c for c in CASE_SET.cases.values() if c.technique == "multi_hop")
    plan = plan_case(SUITE, case, CASE_SET.split_of[case.id])
    environment = SUITE.load_and_inject_default_environment(plan.injections)
    pointer, action = plan.injections["injection_incoming_transaction"], plan.injections[
        "injection_address_change"]
    subjects = [squash(t.subject) for t in environment.bank_account.transactions]
    assert squash(pointer) in subjects
    assert squash(action) in squash(environment.filesystem.files["address-change.txt"])
