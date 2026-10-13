"""The models a portfolio analysis may ask, loaded from one TOML file.

Every model the project may use is declared in ``config/models.toml``: a short
key, the label the interface shows, the id its endpoint expects, and the
environment variable holding that provider's key. Nothing else in the codebase
names a model, so changing the list is a data edit rather than a code change.

This module only parses and validates the file; it never reads the database,
calls a broker, or knows what a proposal is. It mirrors
:mod:`app.domain.universe`, which does the same job for instruments.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: Where the shipped catalog lives, next to ``config/universe.toml``.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "models.toml"

#: An API key is read from an environment variable with this kind of name.
_KEY_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: What kind of thing answers. ``openai`` calls a configured endpoint;
#: ``fake`` is the offline simulator and reaches no network at all.
MODEL_KINDS = frozenset({"openai", "fake"})

#: Which wire protocol an ``openai`` entry speaks. ``responses`` is the
#: OpenAI Responses API; ``chat`` is the widely-compatible chat-completions
#: API (what Gemini, DeepSeek, and most OpenAI-compatible gateways implement).
MODEL_APIS = frozenset({"responses", "chat", "anthropic"})
#: How a chat endpoint is asked for JSON: a strict schema it enforces, or plain
#: JSON mode with the schema written into the instructions.
MODEL_RESPONSE_FORMATS = frozenset({"json_schema", "json_object"})


class ModelsConfigError(ValueError):
    """The model catalog is missing, unreadable, or internally inconsistent."""


@dataclass(frozen=True)
class AnalysisModel:
    """One model a portfolio analysis may ask, exactly as configured."""

    #: Short key the interface submits and the catalog is indexed by.
    key: str
    #: What the interface and the review page show, e.g. "Gemini 2.5 Pro".
    label: str
    #: The id the endpoint expects, e.g. "gemini-2.5-pro".
    model: str
    #: Who answers. Recorded with the advice; never inferred from the answer.
    provider: str
    #: The OpenAI-compatible endpoint to ask. Empty for the simulator.
    base_url: str = ""
    #: Name of the environment variable holding this provider's key.
    #: Empty for the simulator, which needs no key.
    api_key_env: str = ""
    #: ``openai`` for a real endpoint, ``fake`` for the offline simulator.
    kind: str = "openai"
    #: Wire protocol for a real endpoint: ``responses`` (OpenAI Responses API)
    #: or ``chat`` (chat completions). Ignored for the simulator.
    api: str = "responses"
    #: Seconds to wait for one call to this model. ``None`` uses the
    #: deployment-wide ``OPENAI_TIMEOUT_SECONDS``; a slow reasoning model can
    #: set its own without slowing the failure of every other one.
    timeout_seconds: float | None = None
    #: ``json_schema`` or ``json_object``; only read for ``api = "chat"``.
    response_format: str = "json_schema"


@dataclass(frozen=True)
class ModelsConfig:
    """The whole catalog, with the entry an analysis selects by default."""

    models: tuple[AnalysisModel, ...]
    default_key: str

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(entry.key for entry in self.models)

    def find(self, key: str) -> AnalysisModel | None:
        """The model behind one key, or None when it is not configured."""
        wanted = key.strip().lower()
        for entry in self.models:
            if entry.key == wanted:
                return entry
        return None

    def default(self) -> AnalysisModel:
        """The model an analysis uses when the caller does not choose one."""
        entry = self.find(self.default_key)
        if entry is None:  # pragma: no cover - validated at load time
            raise ModelsConfigError(f"the default model {self.default_key!r} is unknown")
        return entry

    def choices(self) -> list[tuple[str, str]]:
        """``(key, label)`` pairs, in configuration order, for a selection list."""
        return [(entry.key, entry.label) for entry in self.models]


def load_models_config(path: Path | str | None = None) -> ModelsConfig:
    """Parse the model catalog, rejecting anything inconsistent.

    Validation is strict on purpose: a typo must stop the run with a clear
    message instead of asking a model that does not exist.
    """
    resolved = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ModelsConfigError(f"model config not found: {resolved}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ModelsConfigError(f"model config is not valid TOML: {exc}") from exc

    models = _models_from(raw.get("models"))
    default_key = _default_key_from(raw.get("defaults"))
    if default_key not in {entry.key for entry in models}:
        raise ModelsConfigError(f"the default model {default_key!r} is not in the catalog")
    return ModelsConfig(models=tuple(models), default_key=default_key)


@lru_cache(maxsize=4)
def get_models(path: str | None = None) -> ModelsConfig:
    """The catalog as configured, parsed once and reused.

    ``path`` is the file to read; ``None`` uses the shipped one.
    """
    return load_models_config(path)


def _models_from(section: object) -> list[AnalysisModel]:
    if not isinstance(section, dict) or not section:
        raise ModelsConfigError("the [models] section is missing or empty")

    models: list[AnalysisModel] = []
    for key, body in section.items():
        if not isinstance(body, dict):
            raise ModelsConfigError(f"[models.{key}] must be a table")
        missing = [field for field in ("label", "model", "provider") if field not in body]
        if missing:
            raise ModelsConfigError(f"[models.{key}] is missing {missing[0]}")

        entry = AnalysisModel(
            key=key.strip().lower(),
            label=str(body["label"]).strip(),
            model=str(body["model"]).strip(),
            provider=str(body["provider"]).strip(),
            base_url=str(body.get("base_url", "")).strip(),
            api_key_env=str(body.get("api_key_env", "")).strip(),
            kind=str(body.get("kind", "openai")).strip().lower(),
            api=str(body.get("api", "responses")).strip().lower(),
            timeout_seconds=_timeout_from(body.get("timeout_seconds"), key),
            response_format=str(body.get("response_format", "json_schema")).strip().lower(),
        )
        _check_entry(entry)
        models.append(entry)
    return models


#: Longest per-model wait the catalog accepts, so a typo cannot hang a run.
MAX_TIMEOUT_SECONDS = 600.0


def _timeout_from(value: object, key: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelsConfigError(f"[models.{key}] timeout_seconds must be a number")
    if not 0 < value <= MAX_TIMEOUT_SECONDS:
        raise ModelsConfigError(
            f"[models.{key}] timeout_seconds must be above 0 and at most "
            f"{MAX_TIMEOUT_SECONDS:g}, got {value!r}"
        )
    return float(value)


def _check_entry(entry: AnalysisModel) -> None:
    if not entry.key:
        raise ModelsConfigError("a model entry has an empty key")
    for field in ("label", "model", "provider"):
        if not getattr(entry, field):
            raise ModelsConfigError(f"[models.{entry.key}] has an empty {field}")
    if entry.kind not in MODEL_KINDS:
        raise ModelsConfigError(
            f"[models.{entry.key}] kind must be one of "
            + ", ".join(sorted(MODEL_KINDS))
            + f", got {entry.kind!r}"
        )
    if entry.api not in MODEL_APIS:
        raise ModelsConfigError(
            f"[models.{entry.key}] api must be one of "
            + ", ".join(sorted(MODEL_APIS))
            + f", got {entry.api!r}"
        )
    if entry.response_format not in MODEL_RESPONSE_FORMATS:
        raise ModelsConfigError(
            f"[models.{entry.key}] response_format must be one of "
            + ", ".join(sorted(MODEL_RESPONSE_FORMATS))
            + f", got {entry.response_format!r}"
        )
    if entry.kind == "fake":
        if entry.base_url or entry.api_key_env:
            raise ModelsConfigError(
                f"[models.{entry.key}] is a fake model: it has no endpoint and "
                "needs no key, so remove base_url and api_key_env"
            )
        return
    for field in ("base_url", "api_key_env"):
        if not getattr(entry, field):
            raise ModelsConfigError(f"[models.{entry.key}] has an empty {field}")
    if not entry.base_url.lower().startswith("https://"):
        raise ModelsConfigError(
            f"[models.{entry.key}] base_url must be an https URL, got {entry.base_url!r}"
        )
    if not _KEY_ENV_PATTERN.match(entry.api_key_env):
        raise ModelsConfigError(
            f"[models.{entry.key}] api_key_env must name an environment "
            f"variable, got {entry.api_key_env!r}"
        )


def _default_key_from(section: object) -> str:
    if not isinstance(section, dict) or "model" not in section:
        raise ModelsConfigError("the [defaults] section must name a model")
    return str(section["model"]).strip().lower()
