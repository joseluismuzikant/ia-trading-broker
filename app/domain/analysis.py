"""The typed contract a portfolio analyst must return.

An analyst — the OpenAI one planned behind :class:`PortfolioAdvisor`, or the
deterministic fallback — answers with advice only: which instrument, what
action, how confident, and why. It never returns a quantity, an order size, or
a broker request. Those are built later by deterministic Python
(:mod:`app.domain.policy`), so no model can size a trade or reach a broker.

Both models are frozen and **forbid extra fields** on purpose. They are the
shape untrusted model output is parsed into: anything the analyst did not
mean to say — including executable fields such as ``quantity``,
``notional`` or ``broker_payload`` — is rejected instead of silently ignored.

Nothing here knows about OpenAI, HTTP, a database, or the broker.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.domain.trading import Action


class AssetAdvice(BaseModel):
    """One instrument's advice: an action, a confidence, and the reason.

    The symbol is normalised to upper case on the way in, because every other
    part of the application compares tickers case-insensitively.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    action: Action
    #: How sure the analyst is of this advice, 0.0 to 1.0. Descriptive only:
    #: it never overrides a risk limit.
    confidence: float
    rationale: str
    #: Known risks the analyst wants shown next to its advice.
    risks: list[str] = Field(default_factory=list)

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalise_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("symbol")
    @classmethod
    def _symbol_is_present(cls, value: str) -> str:
        if not value:
            raise ValueError("symbol must not be empty")
        return value

    @field_validator("confidence")
    @classmethod
    def _confidence_is_a_fraction(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return value

    @field_validator("rationale")
    @classmethod
    def _rationale_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rationale must not be empty")
        return value

    @field_validator("risks", mode="before")
    @classmethod
    def _risks_may_be_missing(cls, value: object) -> object:
        return [] if value is None else value


class PortfolioAdvice(BaseModel):
    """One complete analyst answer: a summary and the advice per instrument.

    ``model_provider``, ``model_name`` and ``analysis_version`` record who
    produced the answer and under which contract, so a saved proposal can be
    read later without guessing which prompt or model made it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str
    decisions: list[AssetAdvice]
    model_provider: str
    model_name: str
    analysis_version: str

    @field_validator("summary", "model_provider", "model_name", "analysis_version")
    @classmethod
    def _text_is_present(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("decisions")
    @classmethod
    def _one_decision_per_symbol(cls, value: list[AssetAdvice]) -> list[AssetAdvice]:
        # AssetAdvice already normalised every symbol to upper case.
        seen: set[str] = set()
        for decision in value:
            if decision.symbol in seen:
                raise ValueError(f"duplicate advice for {decision.symbol}")
            seen.add(decision.symbol)
        return value
