"""Provider table from configs/providers.yaml: base URLs, key env var names, prices, limits."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

DEFAULT_PROVIDERS_PATH = Path(__file__).resolve().parents[2] / "configs" / "providers.yaml"


class _Strict(BaseModel):
    # A typo in the YAML must fail loudly, not silently fall back to a default.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Price(_Strict):
    input_per_mtok: float
    output_per_mtok: float
    source: str
    checked: date

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1e6


class Limits(_Strict):
    rpm: int | None
    rpd: int | None
    tpm: int | None
    tpd: int | None
    source: str
    checked: date
    verified_for_free_plan: bool


ReasoningEffort = Literal["low", "medium", "high"]


class Sampling(_Strict):
    """The temperature and reasoning effort every run of a model uses (DECISIONS.md D19)."""

    temperature: float
    reasoning_effort: ReasoningEffort | None  # None: the parameter is not sent
    source: str
    checked: date


class StructuredOutputs(_Strict):
    """The model accepts `response_format` {"type": "json_schema", ...} (D62). `strict`: the
    provider decodes under the schema's constraints, so the reply always matches it."""

    strict: bool
    source: str
    checked: date


class ModelInfo(_Strict):
    tool_calling: bool
    price: Price
    limits: Limits
    sampling: Sampling | None = None
    structured_outputs: StructuredOutputs | None = None  # None: not documented, never sent


class ProviderConfig(_Strict):
    name: str
    base_url: str
    base_url_source: str
    api_key_env: str | None  # None: the provider needs no key (local Ollama)
    models: dict[str, ModelInfo]


def load_providers(path: Path = DEFAULT_PROVIDERS_PATH) -> dict[str, ProviderConfig]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        name: ProviderConfig(name=name, **body) for name, body in raw["providers"].items()
    }
