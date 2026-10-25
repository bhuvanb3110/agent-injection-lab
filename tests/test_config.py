"""Experiment configs: loading, validation, and expansion into RunSpecs."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lab.config import ExperimentConfig, expand, load_experiment
from lab.models.providers import load_providers

EXPERIMENTS = Path(__file__).resolve().parents[1] / "configs" / "experiments"


def test_baseline_is_16_tasks_by_both_groq_models_once() -> None:
    config = load_experiment(EXPERIMENTS / "baseline_banking.yaml")
    specs = expand(config, load_providers())
    assert len(specs) == 32 and len({s.run_id for s in specs}) == 32
    assert {s.model_id for s in specs} == {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}
    assert {s.user_task_id for s in specs} == {f"user_task_{i}" for i in range(16)}
    assert all(s.max_tokens == 2048 for s in specs)
    # Sampling comes from providers.yaml (D19), the seed from the config (D27).
    assert all((s.temperature, s.reasoning_effort) == (0.6, "medium") for s in specs)
    assert {s.seed for s in specs} == {config.seed}
    # Models are interleaved, so a daily cap leaves both at the same point in the task list.
    assert [s.model_id for s in specs[:2]] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]


def test_smoke_is_3_tasks_on_fake_models() -> None:
    config = load_experiment(EXPERIMENTS / "smoke.yaml")
    specs = expand(config, load_providers())
    assert {s.provider for s in specs} == {"fake"}
    assert len({s.user_task_id for s in specs}) == 3


def test_seed_is_base_plus_repeat() -> None:
    config = load_experiment(EXPERIMENTS / "smoke.yaml").model_copy(update={"repeats": 3})
    specs = expand(config, load_providers())
    assert {(s.repeat_idx, s.seed) for s in specs} == {(0, 1000), (1, 1001), (2, 1002)}


def base() -> dict[str, object]:
    return {
        "name": "x", "suite": "banking", "topology": "t1", "user_tasks": ["user_task_0"],
        "models": [{"provider": "fake", "model_id": "refuse"}], "repeats": 1, "seed": 1,
        "max_tokens": 2048,
    }


def test_typos_and_unknown_values_fail_loudly() -> None:
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(base() | {"temprature": 0.0})  # extra field
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(base() | {"topology": "t9"})
    with pytest.raises(ValueError, match="Unknown banking user tasks"):
        ExperimentConfig.model_validate(base() | {"user_tasks": ["user_task_99"]}).task_ids()
    bad_model = base() | {"models": [{"provider": "groq", "model_id": "nope"}]}
    with pytest.raises(ValueError, match="not in configs/providers.yaml"):
        expand(ExperimentConfig.model_validate(bad_model), load_providers())
    guard = base() | {
        "models": [{"provider": "groq", "model_id": "meta-llama/llama-prompt-guard-2-22m"}]
    }
    with pytest.raises(ValueError, match="no tool calling"):
        expand(ExperimentConfig.model_validate(guard), load_providers())


def test_config_hash_changes_with_any_field() -> None:
    a = ExperimentConfig.model_validate(base())
    assert a.config_hash == ExperimentConfig.model_validate(base()).config_hash
    assert a.config_hash != a.model_copy(update={"max_tokens": 1024}).config_hash
