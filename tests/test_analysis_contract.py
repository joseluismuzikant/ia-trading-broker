"""Tests for the structured OpenAI analysis contract.

These models are what untrusted model output is parsed into, so the tests care
mostly about what they refuse: an empty symbol, a confidence outside 0-1, an
action that is not BUY/HOLD/SELL, or any executable field such as a quantity or
a broker payload.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.analysis import AssetAdvice, PortfolioAdvice


def asset_payload(**overrides: object) -> dict:
    payload: dict = {
        "symbol": "GGAL",
        "action": "BUY",
        "confidence": 0.7,
        "rationale": "The trend is up and momentum agrees.",
    }
    payload.update(overrides)
    return payload


def portfolio_payload(*decisions: AssetAdvice, **overrides: object) -> dict:
    payload: dict = {
        "summary": "Steady portfolio, one addition.",
        "decisions": [decision.model_dump() for decision in decisions],
        "model_provider": "openai",
        "model_name": "gpt-test",
        "analysis_version": "2026-10-01",
    }
    payload.update(overrides)
    return payload


def asset(**overrides: object) -> AssetAdvice:
    return AssetAdvice.model_validate(asset_payload(**overrides))


# --- AssetAdvice ----------------------------------------------------------


def test_valid_asset_advice_parses() -> None:
    advice = asset()

    assert advice.symbol == "GGAL"
    assert advice.action == "BUY"
    assert advice.confidence == 0.7
    assert advice.rationale == "The trend is up and momentum agrees."
    assert advice.risks == []


def test_the_symbol_is_normalised_to_upper_case() -> None:
    advice = AssetAdvice.model_validate(asset_payload(symbol="  ggal  "))

    assert advice.symbol == "GGAL"


def test_every_action_of_the_reused_action_set_is_accepted() -> None:
    for action in ("BUY", "HOLD", "SELL"):
        assert asset(action=action).action == action


def test_an_empty_symbol_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(symbol=""))


def test_a_whitespace_only_symbol_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(symbol="   "))


def test_a_confidence_below_zero_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(confidence=-0.01))


def test_a_confidence_above_one_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(confidence=1.01))


@pytest.mark.parametrize("action", ["", "buy", "BUY_MORE", "REDUCE", "STRONG_BUY"])
def test_an_unsupported_action_is_rejected(action: str) -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(action=action))


def test_an_empty_rationale_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(rationale=""))


def test_a_whitespace_only_rationale_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(rationale="   \n\t "))


@pytest.mark.parametrize(
    "field",
    ["quantity", "order_size", "notional", "limit_price", "broker_payload",
     "broker_order", "credentials"],
)
def test_an_execution_field_is_rejected(field: str) -> None:
    # A model must never be able to describe an executable trade.
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(**{field: 100}))


def test_a_missing_field_is_rejected() -> None:
    for field in ("symbol", "action", "confidence", "rationale"):
        payload = asset_payload()
        del payload[field]
        with pytest.raises(ValidationError):
            AssetAdvice.model_validate(payload)


def test_risks_default_to_an_empty_list() -> None:
    assert AssetAdvice.model_validate(asset_payload()).risks == []
    assert AssetAdvice.model_validate(asset_payload(risks=None)).risks == []


def test_risks_are_kept_as_written() -> None:
    advice = AssetAdvice.model_validate(asset_payload(risks=["Thin market"]))

    assert advice.risks == ["Thin market"]


def test_the_advice_is_immutable() -> None:
    advice = asset()

    with pytest.raises(ValidationError):
        advice.confidence = 0.9


# --- PortfolioAdvice ------------------------------------------------------


def test_valid_portfolio_advice_parses() -> None:
    advice = PortfolioAdvice.model_validate(portfolio_payload(asset()))

    assert advice.summary == "Steady portfolio, one addition."
    assert [decision.symbol for decision in advice.decisions] == ["GGAL"]
    assert advice.model_provider == "openai"
    assert advice.model_name == "gpt-test"
    assert advice.analysis_version == "2026-10-01"


def test_duplicate_symbol_decisions_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(
            portfolio_payload(asset(), asset(action="HOLD"))
        )


def test_duplicates_are_caught_after_symbol_normalisation() -> None:
    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(
            portfolio_payload(asset(symbol="ggal"), asset(symbol=" GGAL "))
        )


@pytest.mark.parametrize("field", ["summary", "model_provider", "model_name", "analysis_version"])
def test_empty_provenance_and_summary_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(portfolio_payload(asset(), **{field: ""}))


def test_an_empty_summary_of_spaces_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(portfolio_payload(asset(), summary="  "))


def test_an_unexpected_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(
            portfolio_payload(asset(), model_temperature=0.2)
        )


def test_a_decisions_payload_without_fields_is_rejected() -> None:
    payload = portfolio_payload()
    payload["decisions"] = [{"symbol": "GGAL"}]

    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(payload)


def test_a_missing_portfolio_field_is_rejected() -> None:
    for field in ("summary", "decisions", "model_provider", "model_name", "analysis_version"):
        payload = portfolio_payload(asset())
        del payload[field]
        with pytest.raises(ValidationError):
            PortfolioAdvice.model_validate(payload)


def test_the_portfolio_advice_is_immutable() -> None:
    advice = PortfolioAdvice.model_validate(portfolio_payload(asset()))

    with pytest.raises(ValidationError):
        advice.summary = "Something else."
