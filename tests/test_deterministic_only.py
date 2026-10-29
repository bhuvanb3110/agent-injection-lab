"""D108: the defense sweep's "deterministic only" combination is T4, so it reuses T4's runs.

The combination is the one BRIEF §6's deterministic family leaves in Banking: `policy_v1` at the
action checkpoint on T2's base, with no amount caps and no egress allowlist (PLAN_M3 §0a).
"""

from lab.defenses.policy import Policy, PolicyGate, load_policy
from lab.models.fake import FakeModel
from lab.models.llm_element import LabLLM
from lab.topologies.presets import POLICY_V1, PRESETS, Preset, build_gates, parse_components

DETERMINISTIC_ONLY = Preset("planner_workers", (f"{POLICY_V1}@action",))


def spec_of(preset: Preset) -> tuple[str, tuple[str, ...]]:
    """What a pipeline is made of: its base and its components (the parent is only bookkeeping)."""
    return (preset.base, preset.components)


def test_deterministic_only_has_the_t4_pipeline_spec() -> None:
    assert spec_of(DETERMINISTIC_ONLY) == spec_of(PRESETS["t4"])
    assert spec_of(DETERMINISTIC_ONLY) != spec_of(PRESETS["t2"])
    assert parse_components(DETERMINISTIC_ONLY.components) == parse_components(
        PRESETS["t4"].components
    ) == {"policy_v1": frozenset({"action"})}


def test_it_builds_the_same_gates_as_t4() -> None:
    llm = LabLLM(FakeModel([]), provider="fake", model_id="scripted")
    mine = build_gates(DETERMINISTIC_ONLY.components, llm)
    t4 = build_gates(PRESETS["t4"].components, llm)
    assert len(mine) == len(t4) == 1
    for a, b in zip(mine, t4, strict=True):
        assert isinstance(a, PolicyGate) and isinstance(b, PolicyGate)
        assert (a.name, a.checkpoints, a.policy) == (b.name, b.checkpoints, b.policy)
        assert a.policy == load_policy()


def test_policy_v1_has_no_amount_cap() -> None:
    """The reason the combination is T4 (D108): no rule of policy_v1 looks at an amount."""
    assert set(Policy.model_fields) == {"name", "suite", "recipients", "password", "user_info"}
    assert PRESETS["t4"].parent == "t2"
