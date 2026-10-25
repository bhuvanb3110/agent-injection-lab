"""configs/providers.yaml loads and every number in it says where it came from."""

from lab.models.providers import load_providers


def test_groq_and_ollama_are_configured() -> None:
    providers = load_providers()
    assert set(providers) == {"groq", "ollama"}
    assert providers["groq"].api_key_env == "GROQ_API_KEY"
    assert providers["ollama"].api_key_env is None


def test_every_model_has_sourced_prices_and_limits() -> None:
    for provider in load_providers().values():
        assert provider.models
        for model in provider.models.values():
            assert model.tool_calling
            assert model.price.source and model.limits.source
            assert model.price.input_per_mtok >= 0 and model.price.output_per_mtok >= 0
