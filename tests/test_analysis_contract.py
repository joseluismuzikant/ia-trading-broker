"""Tests for the portfolio-analysis contract: input context and output advice.

The output models are what untrusted model output is parsed into, so the tests
care mostly about what they refuse: executable fields such as a quantity, a
confidence outside 0-1, or a target weight outside 0-1.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.analysis import AssetAdvice, PortfolioAdvice


def asset_payload(**overrides: object) -> dict:
    payload: dict = {
        "symbol": "YPFD",
        "action": "HOLD",
        "target_weight": 0.25,
        "confidence": 0.7,
        "rationale": "Keep the current share of the portfolio.",
    }
    payload.update(overrides)
    return payload


def portfolio_payload(*decisions: AssetAdvice, **overrides: object) -> dict:
    payload: dict = {
        "summary": "Keep the portfolio, raise cash a little.",
        "target_cash_weight": 0.25,
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

    assert advice.symbol == "YPFD"
    assert advice.action == "HOLD"
    assert advice.target_weight == 0.25
    assert advice.confidence == 0.7
    assert advice.risks == []


def test_the_symbol_is_normalised_to_upper_case() -> None:
    assert AssetAdvice.model_validate(asset_payload(symbol="  ypfD ")).symbol == "YPFD"


def test_every_action_of_the_reused_action_set_is_accepted() -> None:
    for action in ("BUY", "HOLD", "SELL"):
        assert asset(action=action).action == action


def test_a_zero_target_weight_is_a_complete_exit() -> None:
    assert asset(action="SELL", target_weight=0.0).target_weight == 0.0


@pytest.mark.parametrize("weight", [-0.01, 1.01])
def test_a_target_weight_outside_zero_one_is_rejected(weight: float) -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(target_weight=weight))


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_a_confidence_outside_zero_one_is_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(confidence=confidence))


@pytest.mark.parametrize("action", ["", "buy", "BUY_MORE", "REDUCE", "STRONG_BUY"])
def test_an_unsupported_action_is_rejected(action: str) -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(action=action))


@pytest.mark.parametrize("field", ["", "   "])
def test_an_empty_symbol_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(symbol=field))


def test_an_empty_rationale_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(rationale="  "))


@pytest.mark.parametrize(
    "field",
    [
        "quantity",
        "shares",
        "order_size",
        "notional",
        "limit_price",
        "broker_payload",
        "broker_request",
        "credentials",
        "order",
    ],
)
def test_an_execution_field_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        AssetAdvice.model_validate(asset_payload(**{field: 100}))


def test_a_missing_asset_field_is_rejected() -> None:
    for field in ("symbol", "action", "target_weight", "confidence", "rationale"):
        payload = asset_payload()
        del payload[field]
        with pytest.raises(ValidationError):
            AssetAdvice.model_validate(payload)


def test_the_asset_advice_is_immutable() -> None:
    advice = asset()
    with pytest.raises(ValidationError):
        advice.target_weight = 0.9


# --- PortfolioAdvice ------------------------------------------------------


def test_valid_portfolio_advice_parses() -> None:
    advice = PortfolioAdvice.model_validate(portfolio_payload(asset()))

    assert advice.summary.startswith("Keep the portfolio")
    assert advice.target_cash_weight == 0.25
    assert advice.total_instrument_weight == 0.25
    assert advice.model_provider == "openai"


def test_a_full_allocation_of_the_portfolio_parses() -> None:
    advice = PortfolioAdvice.model_validate(
        portfolio_payload(
            asset(symbol="YPFD", target_weight=0.5),
            asset(symbol="GGAL", target_weight=0.5, action="BUY"),
            target_cash_weight=0.0,
        )
    )
    assert advice.total_instrument_weight + advice.target_cash_weight == 1.0


@pytest.mark.parametrize("weight", [-0.01, 1.01])
def test_a_target_cash_weight_outside_zero_one_is_rejected(weight: float) -> None:
    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(portfolio_payload(asset(), target_cash_weight=weight))


def test_duplicate_symbol_decisions_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(
            portfolio_payload(asset(), asset(action="BUY", target_weight=0.5))
        )


@pytest.mark.parametrize("field", ["summary", "model_provider", "model_name", "analysis_version"])
def test_empty_provenance_and_summary_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(portfolio_payload(asset(), **{field: " "}))


def test_an_unexpected_portfolio_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(portfolio_payload(asset(), model_temperature=0.2))


def test_an_execution_field_is_rejected_on_the_portfolio_too() -> None:
    with pytest.raises(ValidationError):
        PortfolioAdvice.model_validate(
            portfolio_payload(asset(), orders=[{"symbol": "YPFD", "quantity": 1}])
        )


def test_the_portfolio_advice_is_immutable() -> None:
    advice = PortfolioAdvice.model_validate(portfolio_payload(asset()))
    with pytest.raises(ValidationError):
        advice.summary = "Something else."
