"""configs/providers.yaml loads and every number in it says where it came from."""

from lab.models.providers import load_providers

AGENT_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b")
GUARD_MODELS = ("meta-llama/llama-prompt-guard-2-22m", "meta-llama/llama-prompt-guard-2-86m")


def test_groq_and_ollama_are_configured() -> None:
    providers = load_providers()
    assert set(providers) == {"groq", "ollama"}
    assert providers["groq"].api_key_env == "GROQ_API_KEY"
    assert providers["ollama"].api_key_env is None


def test_every_model_has_sourced_prices_and_limits() -> None:
    for provider in load_providers().values():
        assert provider.models
        for model in provider.models.values():
            assert model.price.source and model.limits.source
            assert model.price.input_per_mtok >= 0 and model.price.output_per_mtok >= 0


def test_groq_free_tier_limits_match_the_console() -> None:
    # Values from console.groq.com/settings/limits, checked 2026-10-25 (DECISIONS.md D20).
    models = load_providers()["groq"].models
    for model_id in AGENT_MODELS:
        limits = models[model_id].limits
        assert (limits.rpm, limits.rpd, limits.tpm, limits.tpd) == (30, 1000, 8000, 200_000)
        assert limits.verified_for_free_plan
    for model_id in GUARD_MODELS:
        limits = models[model_id].limits
        assert (limits.rpm, limits.rpd, limits.tpm, limits.tpd) == (30, 14_400, 15_000, 500_000)
        assert not models[model_id].tool_calling


def test_agent_models_pin_sampling() -> None:
    models = load_providers()["groq"].models
    for model_id in AGENT_MODELS:
        sampling = models[model_id].sampling
        assert sampling is not None
        assert models[model_id].tool_calling
        assert sampling.temperature == 0.6
        assert sampling.reasoning_effort == "medium"
