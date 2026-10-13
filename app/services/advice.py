"""The seam between an analysis run and whatever answers for a model.

One function, :func:`build_advisor`, is the only place that reaches a model;
the tests replace it and never spend an API call. Everything else here is
preparation and checking: turning the account into a
:class:`~app.domain.analysis.PortfolioAnalysisContext`, and turning the answer
into the flat view the proposal stores.

A model failure is a run failure. There is no fallback advisor: a missing key
or a refused call stops the run before a proposal exists, so a saved proposal
always records what actually produced it.

Nothing here sizes an order. The advice is a target allocation; the rebalance
that turns it into quantities is separate, deterministic work.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from app.config import get_settings
from app.domain.analysis import PortfolioAdvice, PortfolioAnalysisContext
from app.domain.analysis_models import AnalysisModel, get_models
from app.domain.portfolio import Instrument as BrokerInstrument
from app.domain.portfolio import Portfolio, Position
from app.domain.trading import (
    AssetAdviceView,
    IndicatorEvidence,
    PaperLedger,
    PortfolioAdviceView,
)
from app.domain.universe import UniverseConfig
from app.infrastructure.openai import OpenAIPortfolioAdvisor
from app.infrastructure.simulation import FakePortfolioAdvisor
from app.ports.portfolio_advisor import PortfolioAdvisor
from app.services.advice_validation import validate_portfolio_advice


class AdviceError(ValueError):
    """The run cannot ask a model, or the answer does not fit this account."""


def resolve_model(model_key: str | None = None) -> AnalysisModel:
    """The model one analysis run will ask, or a clear error naming the choice.

    A key from the form wins, then the deployment default, then the catalog's
    own default. An unknown key is refused before anything is read or asked.
    """
    settings = get_settings()
    config = get_models(settings.models_config_path or None)
    key = model_key or settings.analysis_model_default or config.default_key
    entry = config.find(key)
    if entry is None:
        raise AdviceError(
            f"unknown analysis model {key!r}; choose one of " + ", ".join(config.keys)
        )
    return entry


def model_choices() -> list[tuple[str, str]]:
    """``(key, label)`` pairs for the analysis form, in catalog order."""
    settings = get_settings()
    return get_models(settings.models_config_path or None).choices()


def build_advisor(model: AnalysisModel | str | None = None) -> PortfolioAdvisor:
    """The one place that reaches a model.

    This is the seam the tests replace: patching it turns every run offline.
    Without a patch it returns the simulator for a ``fake`` catalog entry — no
    key, no network — or the OpenAI advisor for the chosen model, which
    requires that provider's key to be configured.
    """
    entry = model if isinstance(model, AnalysisModel) else resolve_model(model)
    if entry.kind == "fake":
        return FakePortfolioAdvisor(model=entry)
    return OpenAIPortfolioAdvisor(model=entry)


def offerable_watchlist(
    *,
    ledger: PaperLedger,
    evidence: Mapping[str, IndicatorEvidence],
    universe: UniverseConfig,
) -> tuple[list, list[str]]:
    """The instruments a model may be shown, and why the others are left out.

    Positions exist only in whole units, so an instrument whose single unit
    costs more than the position limit of this account can never be bought,
    and one with no price cannot be sized. Offering them only produces a plan
    Python must refuse, so they are not offered. Anything already held always
    stays: the model must see every holding.
    """
    total = ledger.total_value.amount
    cap = universe.constraints.max_single_position_pct * total
    held = {position.symbol for position in ledger.positions}
    offered = []
    too_dear: list[str] = []
    unpriced: list[str] = []
    for instrument in universe.instruments:
        if instrument.symbol in held:
            offered.append(instrument)
            continue
        reading = evidence.get(instrument.symbol)
        price = reading.last_price if reading is not None else None
        if price is None or not price.is_positive:
            unpriced.append(instrument.symbol)
        elif price.amount / instrument.quotation_factor > cap:
            too_dear.append(instrument.symbol)
        else:
            offered.append(instrument)

    notes: list[str] = []
    limit = f"{float(universe.constraints.max_single_position_pct):.0%}"
    if too_dear:
        notes.append(
            f"Not offered: one unit costs more than the {limit} position limit of this "
            f"account, so it cannot be bought: {', '.join(sorted(too_dear))}."
        )
    if unpriced:
        notes.append(f"Not offered: no price is known: {', '.join(sorted(unpriced))}.")
    return offered, notes


def analysis_context(
    *,
    country: str,
    ledger: PaperLedger,
    evidence: Mapping[str, IndicatorEvidence],
    universe: UniverseConfig,
    warnings: Sequence[str] = (),
    analysed_at: datetime | None = None,
) -> PortfolioAnalysisContext:
    """Describe the account as one whole, in numbers a model may see.

    The account is the paper ledger: the record of cash and positions the plan
    is sized against. It carries no credentials and no broker payload.
    """
    positions = [
        Position(
            instrument=BrokerInstrument(
                symbol=position.symbol,
                market=position.market,
                currency=position.currency,
            ),
            total_quantity=float(position.quantity),
            available_quantity=float(position.quantity),
            average_price=float(position.average_price.amount),
            last_price=float(position.last_price.amount),
            market_value=float(position.market_value.amount),
        )
        for position in ledger.positions
    ]
    return PortfolioAnalysisContext(
        country=country,
        analysed_at=analysed_at or datetime.now(UTC),
        portfolio=Portfolio(
            country=country,
            currency=ledger.currency,
            cash=float(ledger.cash.amount),
            positions=positions,
        ),
        evidence=dict(evidence),
        watchlist=tuple(
            offerable_watchlist(ledger=ledger, evidence=evidence, universe=universe)[0]
        ),
        constraints=universe.constraints,
        warnings=tuple(warnings),
    )


def validated_view(
    advice: PortfolioAdvice,
    *,
    model: AnalysisModel,
    context: PortfolioAnalysisContext,
    universe: UniverseConfig,
) -> PortfolioAdviceView:
    """Check the advice against this account, then flatten it for the proposal.

    Validation is not optional: an answer that forgets a holding, invents a
    symbol, or spends money that is not there is refused here, before a
    proposal can record it.
    """
    try:
        validate_portfolio_advice(
            advice,
            held_symbols=context.held_symbols,
            universe=universe,
            current_weights=context.current_weights,
            min_cash_pct=float(universe.constraints.min_cash_pct),
        )
    except ValueError as exc:
        raise AdviceError(str(exc)) from exc

    by_symbol = {instrument.symbol: instrument for instrument in universe.instruments}
    decisions = [
        AssetAdviceView(
            symbol=decision.symbol,
            name=(by_symbol[decision.symbol].name if decision.symbol in by_symbol else ""),
            category=(by_symbol[decision.symbol].category if decision.symbol in by_symbol else ""),
            action=decision.action,
            target_weight=decision.target_weight,
            current_weight=context.weight_of(decision.symbol),
            confidence=decision.confidence,
            rationale=decision.rationale,
            risks=list(decision.risks),
        )
        for decision in advice.decisions
    ]
    return PortfolioAdviceView(
        summary=advice.summary,
        target_cash_weight=advice.target_cash_weight,
        current_cash_weight=context.cash_weight,
        model_label=model.label,
        model_provider=advice.model_provider,
        model_name=advice.model_name,
        analysis_version=advice.analysis_version,
        decisions=decisions,
    )
