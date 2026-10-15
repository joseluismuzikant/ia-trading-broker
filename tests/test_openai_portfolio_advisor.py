"""Tests for the OpenAI portfolio advisor and its client.

Everything here runs against a fake: no test may spend an OpenAI call. What is
checked is the boundary — one call for the whole portfolio, the wording that
forbids assuming new money, provenance stamped from the configured catalog
entry rather than trusted from the model, and a client that refuses to run
without that provider's key.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.domain.analysis import PortfolioAdvice, PortfolioAnalysisContext
from app.domain.analysis_models import AnalysisModel
from app.domain.portfolio import Instrument as BrokerInstrument
from app.domain.portfolio import Portfolio, Position
from app.domain.universe import Instrument, PortfolioConstraints
from app.infrastructure.openai import OpenAIClient, OpenAIClientError, OpenAIPortfolioAdvisor
from app.infrastructure.openai.client import failure_message, strict_json_schema
from app.infrastructure.openai.prompts import ANALYSIS_PROMPT_VERSION, SYSTEM_PROMPT
from app.services.advice_validation import validate_portfolio_advice


def catalog_entry(**overrides) -> AnalysisModel:
    payload = {
        "key": "gemini_2_5_pro",
        "label": "Gemini 2.5 Pro",
        "model": "gemini-2.5-pro",
        "provider": "google",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
    }
    payload.update(overrides)
    return AnalysisModel(**payload)


class FakeOpenAIClient:
    """Answers with a canned payload and remembers how it was asked."""

    model = "gemini-2.5-pro"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def complete_json(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return self.payload


class _Responses:
    def __init__(self, text: str) -> None:
        self._text = text
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return type("Response", (), {"output_text": self._text})()


class FakeTransport:
    """Stands in for the SDK client: ``client.responses.create(...)``."""

    def __init__(self, text: str) -> None:
        self.responses = _Responses(text)


def build_context() -> PortfolioAnalysisContext:
    """The 100,000 portfolio from the design notes."""
    return PortfolioAnalysisContext(
        country="argentina",
        analysed_at=datetime(2026, 10, 2, 14, 42, 6, tzinfo=UTC),
        portfolio=Portfolio(
            country="argentina",
            currency="Peso_Argentino",
            cash=40_000.0,
            positions=[
                Position(
                    instrument=BrokerInstrument(symbol="YPFD", description="YPF S.A."),
                    total_quantity=100.0,
                    average_price=200.0,
                    last_price=250.0,
                    market_value=25_000.0,
                ),
                Position(
                    instrument=BrokerInstrument(symbol="GGAL"),
                    total_quantity=200.0,
                    last_price=100.0,
                    market_value=20_000.0,
                ),
                Position(
                    instrument=BrokerInstrument(symbol="AL30"),
                    total_quantity=150.0,
                    last_price=100.0,
                    market_value=15_000.0,
                ),
            ],
        ),
        evidence={},
        watchlist=(
            Instrument("VIST", "Vista Energy S.A.", "argentina_stocks"),
            Instrument("SPY", "SPDR S&P 500 ETF Trust", "cedears"),
        ),
        constraints=PortfolioConstraints(),
        warnings=["AL30 history was not refreshed."],
    )


def canned_payload() -> dict:
    """The design-notes target, with provenance the adapter must overwrite."""
    return {
        "summary": "Trim the two financials, add VIST and SPY.",
        "target_cash_weight": 0.10,
        "decisions": [
            {
                "symbol": "YPFD",
                "action": "SELL",
                "target_weight": 0.20,
                "confidence": 0.6,
                "rationale": "Take some profit.",
                "risks": ["FX"],
            },
            {
                "symbol": "GGAL",
                "action": "SELL",
                "target_weight": 0.10,
                "confidence": 0.6,
                "rationale": "Concentration too high.",
                "risks": [],
            },
            {
                "symbol": "AL30",
                "action": "HOLD",
                "target_weight": 0.15,
                "confidence": 0.7,
                "rationale": "Keep the bond sleeve.",
                "risks": [],
            },
            {
                "symbol": "VIST",
                "action": "BUY",
                "target_weight": 0.20,
                "confidence": 0.5,
                "rationale": "Add energy.",
                "risks": ["Oil"],
            },
            {
                "symbol": "SPY",
                "action": "BUY",
                "target_weight": 0.25,
                "confidence": 0.8,
                "rationale": "Broaden abroad.",
                "risks": [],
            },
        ],
        "model_provider": "made-up",
        "model_name": "made-up",
        "analysis_version": "made-up",
    }


# --- The advisor ----------------------------------------------------------


async def test_one_call_covers_the_whole_portfolio() -> None:
    client = FakeOpenAIClient(canned_payload())

    await OpenAIPortfolioAdvisor(model=catalog_entry(), client=client).analyze(build_context())

    assert len(client.calls) == 1
    assert client.calls[0]["name"] == "portfolio_advice"


async def test_the_prompt_forbids_assuming_new_money() -> None:
    client = FakeOpenAIClient(canned_payload())

    await OpenAIPortfolioAdvisor(model=catalog_entry(), client=client).analyze(build_context())

    instructions = client.calls[0]["instructions"]
    assert "You are modifying an existing portfolio." in instructions
    assert "Do not assume the user adds capital." in instructions
    assert (
        "The current total portfolio value is the complete pool of capital available."
        in instructions
    )
    assert "Return a complete target allocation." in instructions
    assert "Every existing holding must appear." in instructions
    assert "New positions may only come from the supplied watchlist." in instructions
    assert "equal 100%." in instructions
    assert "Respect the minimum cash requirement." in instructions
    assert "Do not provide order quantities or broker requests." in instructions
    assert "Use only the supplied evidence." in instructions
    assert "Do not invent news, fundamentals, or unavailable data." in instructions


async def test_the_request_describes_the_account_and_nothing_sensitive() -> None:
    client = FakeOpenAIClient(canned_payload())

    await OpenAIPortfolioAdvisor(model=catalog_entry(), client=client).analyze(build_context())

    request = client.calls[0]["input_text"]
    assert "40000" in request
    assert "25000" in request
    assert "0.25" in request
    assert "VIST" in request and "SPY" in request
    assert "min_cash_pct" in request
    assert "AL30 history was not refreshed." in request
    for secret in ("password", "api_key", "bearer", "token"):
        assert secret not in request.lower()


async def test_the_answer_is_stamped_with_the_catalog_entry() -> None:
    entry = catalog_entry()
    client = FakeOpenAIClient(canned_payload())

    advice = await OpenAIPortfolioAdvisor(model=entry, client=client).analyze(build_context())

    assert advice.model_provider == "google"
    assert advice.model_name == "gemini-2.5-pro"
    assert advice.analysis_version == ANALYSIS_PROMPT_VERSION


async def test_the_answer_parses_into_a_valid_allocation() -> None:
    client = FakeOpenAIClient(canned_payload())
    advisor = OpenAIPortfolioAdvisor(model=catalog_entry(), client=client)
    view = build_context()

    advice = await advisor.analyze(view)

    assert isinstance(advice, PortfolioAdvice)
    assert advice.total_instrument_weight + advice.target_cash_weight == pytest.approx(1.0)
    validate_portfolio_advice(
        advice,
        held_symbols=view.held_symbols,
        universe=_universe_for(view),
        current_weights=view.current_weights,
        min_cash_pct=0.10,
    )


async def test_the_schema_is_sent_for_structured_output() -> None:
    client = FakeOpenAIClient(canned_payload())

    await OpenAIPortfolioAdvisor(model=catalog_entry(), client=client).analyze(build_context())

    schema = client.calls[0]["schema"]
    assert "target_cash_weight" in schema["properties"]
    assert "decisions" in schema["properties"]


async def test_malformed_model_output_never_becomes_advice() -> None:
    bad = canned_payload()
    bad["decisions"][0]["quantity"] = 100  # an executable field
    client = FakeOpenAIClient(bad)

    with pytest.raises(Exception):
        await OpenAIPortfolioAdvisor(model=catalog_entry(), client=client).analyze(build_context())


# --- The client -----------------------------------------------------------


def test_the_client_needs_the_key_the_entry_names() -> None:
    settings = Settings(
        _env_file=None,
        secret_key="a-development-secret-of-sufficient-length",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    assert settings.gemini_api_key is None

    with pytest.raises(OpenAIClientError, match="GEMINI_API_KEY"):
        OpenAIClient.from_model(catalog_entry(), settings)


def test_the_client_takes_the_model_and_endpoint_from_the_entry() -> None:
    settings = Settings(
        _env_file=None,
        secret_key="a-development-secret-of-sufficient-length",
        database_url="sqlite+aiosqlite:///:memory:",
        gemini_api_key="sk-test",
    )
    entry = catalog_entry()

    client = OpenAIClient.from_model(entry, settings)

    assert client.model == "gemini-2.5-pro"
    assert client._base_url == entry.base_url  # noqa: SLF001 - the point of the test
    assert "sk-test" not in repr(client)


def test_a_model_timeout_overrides_the_deployment_default() -> None:
    from dataclasses import replace

    settings = Settings(
        _env_file=None,
        secret_key="a-development-secret-of-sufficient-length",
        database_url="sqlite+aiosqlite:///:memory:",
        gemini_api_key="sk-test",
        openai_timeout_seconds=30,
    )
    entry = catalog_entry()

    default = OpenAIClient.from_model(entry, settings)
    slow = OpenAIClient.from_model(replace(entry, timeout_seconds=180.0), settings)

    assert default._timeout_seconds == 30  # noqa: SLF001
    assert slow._timeout_seconds == 180.0  # noqa: SLF001


def test_the_advisor_with_a_fake_client_needs_no_key() -> None:
    advisor = OpenAIPortfolioAdvisor(
        model=catalog_entry(), client=FakeOpenAIClient(canned_payload())
    )
    assert advisor.model.label == "Gemini 2.5 Pro"


def test_the_client_asks_for_structured_json() -> None:
    transport = FakeTransport(
        '{"summary": "ok", "target_cash_weight": 1.0, '
        '"decisions": [], "model_provider": "x", '
        '"model_name": "y", "analysis_version": "z"}'
    )
    client = OpenAIClient(
        api_key="sk-test",
        model="gemini-2.5-pro",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        transport=transport,
    )

    import asyncio

    payload = asyncio.run(
        client.complete_json(
            instructions="do the thing",
            input_text="the account",
            schema=PortfolioAdvice.model_json_schema(),
        )
    )

    assert payload["summary"] == "ok"
    sent = transport.responses.kwargs
    assert sent["model"] == "gemini-2.5-pro"
    assert sent["instructions"] == "do the thing"
    assert sent["input"] == "the account"
    assert sent["text"]["format"]["type"] == "json_schema"
    assert sent["text"]["format"]["strict"] is True
    assert "properties" in sent["text"]["format"]["schema"]


def test_a_transport_failure_is_reported_clearly() -> None:
    class Broken:
        class responses:  # noqa: N801 - stands in for the SDK's attribute
            @staticmethod
            async def create(**kwargs):
                raise RuntimeError("connection refused")

    client = OpenAIClient(api_key="sk-test", model="m", transport=Broken())

    import asyncio

    with pytest.raises(OpenAIClientError, match="connection refused"):
        asyncio.run(client.complete_json(instructions="x", input_text="y", schema={}))


def test_the_strict_schema_forbids_extra_fields() -> None:
    strict = strict_json_schema(PortfolioAdvice.model_json_schema())

    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == set(strict["properties"])
    asset = strict["$defs"]["AssetAdvice"]
    assert asset["additionalProperties"] is False
    assert set(asset["required"]) == set(asset["properties"])


def test_the_prompt_version_is_recorded_in_the_prompt() -> None:
    assert ANALYSIS_PROMPT_VERSION in SYSTEM_PROMPT


def _universe_for(view: PortfolioAnalysisContext):
    from app.domain.universe import UniverseConfig

    held = [
        Instrument(symbol, f"{symbol} S.A.", "argentina_stocks")
        for symbol in sorted(view.held_symbols)
    ]
    return UniverseConfig(instruments=tuple(held) + tuple(view.watchlist))


# --- Failure messages ------------------------------------------------------


def _chain(*messages: str) -> Exception:
    """An exception chain shaped like the SDK's own: outer to inner."""
    inner: Exception | None = None
    for message in reversed(messages):
        exc = OSError(message) if inner is None else Exception(message)
        if inner is not None:
            exc.__cause__ = inner
        inner = exc
    return inner  # type: ignore[return-value]


def test_a_dns_failure_names_the_endpoint_and_the_catalog() -> None:
    exc = _chain(
        "Connection error.",
        "[Errno -2] Name or service not known",
    )

    message = failure_message(exc, "https://api.xiaomi.com/v1", 30.0)

    assert "api.xiaomi.com" in message
    assert "does not resolve" in message
    assert "config/models.toml" in message


def test_a_timeout_names_the_endpoint_and_the_limit() -> None:
    message = failure_message(Exception("Request timed out."), "https://api.openai.com/v1", 12.5)

    assert "api.openai.com" in message
    assert "timed out after 12.5s" in message


def test_any_other_failure_still_names_the_endpoint() -> None:
    message = failure_message(Exception("connection refused"), "https://api.x.ai/v1", 30.0)

    assert "api.x.ai" in message
    assert "connection refused" in message


def test_a_rejected_key_names_the_env_var_and_the_provider_reason() -> None:
    inner = Exception("Error code: 401")
    inner.body = {"error": {"message": "Invalid API key"}}
    outer = Exception("Connection error.")
    outer.__cause__ = inner

    message = failure_message(outer, "https://api.xiaomimimo.com/v1", 30.0, key_env="MIMO_API_KEY")

    assert "api.xiaomimimo.com" in message
    assert "MIMO_API_KEY" in message
    assert "Invalid API key" in message
    assert "another provider" in message
    assert "xai" not in message


def test_a_server_error_includes_the_provider_explanation() -> None:
    class FakeResponse:
        @staticmethod
        def json():
            return {"error": {"message": "model not found: mimo-v2.6-pro"}}

    inner = Exception("Error code: 500")
    inner.response = FakeResponse()
    outer = Exception("Connection error.")
    outer.__cause__ = inner

    message = failure_message(outer, "https://api.xiaomimimo.com/v1", 30.0)

    assert "api.xiaomimimo.com" in message
    assert "model not found: mimo-v2.6-pro" in message


def test_an_unauthenticated_failure_without_a_body_still_points_at_the_key() -> None:
    message = failure_message(
        Exception("Client error '401 Unauthorized'"),
        "https://api.xiaomimimo.com/v1",
        30.0,
    )

    assert "API key" in message
    assert "api.xiaomimimo.com" in message


def test_the_prompt_tells_the_model_what_one_whole_unit_costs() -> None:
    import json
    from dataclasses import replace

    from app.domain.money import money
    from app.domain.trading import IndicatorEvidence
    from app.infrastructure.openai.prompts import user_message

    context = replace(
        build_context(),
        evidence={"SPY": IndicatorEvidence(last_price=money("20000.00"))},
    )

    payload = json.loads(user_message(context))

    total = payload["portfolio"]["total_value"]
    spy = next(item for item in payload["watchlist"] if item["symbol"] == "SPY")
    assert spy["unit_cost"] == 20000.0
    assert spy["one_unit_weight"] == round(20000.0 / total, 6)
    # No reading means no price, so the model is told not to add it.
    vist = next(item for item in payload["watchlist"] if item["symbol"] == "VIST")
    assert vist["unit_cost"] is None
    assert vist["one_unit_weight"] is None
    ypfd = next(item for item in payload["portfolio"]["positions"] if item["symbol"] == "YPFD")
    assert ypfd["unit_cost"] == 250.0


def test_a_bond_unit_cost_divides_the_quote_by_its_factor() -> None:
    from app.infrastructure.openai.prompts import _unit_terms

    terms = _unit_terms(85_800.0, 100, 1_000_000.0)

    assert terms["unit_cost"] == 858.0
    assert terms["one_unit_weight"] == 0.000858


def test_the_system_prompt_states_the_whole_unit_rule() -> None:
    assert "whole units" in SYSTEM_PROMPT
    assert "one_unit_weight" in SYSTEM_PROMPT


def test_corrections_reach_the_model_only_when_there_are_some() -> None:
    import json
    from dataclasses import replace

    from app.infrastructure.openai.prompts import user_message

    first = json.loads(user_message(build_context()))
    retry = json.loads(
        user_message(
            replace(build_context(), corrections=("VIST: a target of 0.6% cannot be traded",))
        )
    )

    assert "corrections" not in first
    assert retry["corrections"] == ["VIST: a target of 0.6% cannot be traded"]


def test_the_system_prompt_explains_corrections() -> None:
    assert '"corrections"' in SYSTEM_PROMPT
    assert "answer again" in SYSTEM_PROMPT
