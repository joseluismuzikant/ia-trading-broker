"""Analysis: one portfolio-wide model answer, validated and turned into a plan.

The active path is the MVP rebalance flow: fresh broker portfolio, a new paper
ledger, indicator evidence, one model call for the whole portfolio, validation
of its target allocation, then a deterministic rebalance
(:mod:`app.services.rebalance`) whose exact trades are applied to the paper
preview and saved as the proposal a human reviews.

Only the history refresh costs IOL calls: one price-history read per configured
instrument. Everything after it runs on the saved snapshots.

This service does not place an order.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.analysis import PortfolioAdvice, PortfolioAnalysisContext
from app.domain.analysis_models import AnalysisModel
from app.domain.indicators import (
    Bar,
    atr,
    ema,
    latest,
    macd,
    returns,
    rsi,
    volume_average,
)
from app.domain.money import Money, money
from app.domain.trading import (
    IndicatorEvidence,
    PaperLedger,
    PortfolioAdviceView,
    Proposal,
    RiskCheck,
)
from app.domain.universe import DEFAULT_MARKET, UniverseConfig, get_universe
from app.infrastructure.iol import price_bar_from_quote
from app.infrastructure.iol.schemas import Quote
from app.models import (
    EVENT_PROPOSAL,
    BrokerConnection,
    ProposalRecord,
    price_history_kind,
)
from app.ports.broker_reader import PriceBar
from app.services import advice as advice_service
from app.services import approvals as approvals_service
from app.services import broker as broker_service
from app.services import events as events_service
from app.services import orders as orders_service
from app.services import rebalance as rebalance_service
from app.services import snapshots as snapshots_service

logger = logging.getLogger("ia_trading_broker.analysis")

#: Indicator windows. They match the defaults the strategy's rules assume.
EMA_FAST = 12
EMA_SLOW = 26
RSI_PERIOD = 14
ATR_PERIOD = 14
RETURN_PERIOD = 20
VOLUME_PERIOD = 20


class AnalysisError(Exception):
    """The analysis could not be completed."""


def load_config() -> UniverseConfig:
    """The configured universe and portfolio limits, from the settings' path."""
    path = get_settings().universe_config_path or None
    return get_universe(path)


def _price_bar(item: PriceBar | Quote) -> PriceBar:
    """A price bar, whichever shape the caller has.

    The broker port returns :class:`PriceBar`. An IOL quote row (older saved
    data, and tests that build one) is converted rather than rejected.
    """
    return item if isinstance(item, PriceBar) else price_bar_from_quote(item)


def evidence_for(quotes: Sequence[PriceBar | Quote]) -> IndicatorEvidence:
    """Compute the indicator readings for one instrument's price history.

    Bars arrive newest-first or oldest-first depending on the broker, so they
    are ordered by their timestamp before anything is calculated. A history that
    is too short produces readings of ``None``, which the strategy treats as "not
    enough evidence" rather than as a trade.
    """
    ordered = sorted((_price_bar(item) for item in quotes), key=lambda bar: bar.at or "")
    closes = [bar.close for bar in ordered if bar.close]
    bars = [
        Bar(
            close=bar.close,
            high=bar.high if bar.high is not None else bar.close,
            low=bar.low if bar.low is not None else bar.close,
            volume=bar.volume,
        )
        for bar in ordered
        if bar.close
    ]

    momentum = latest(macd(closes))
    return IndicatorEvidence(
        last_price=money(closes[-1]) if closes else None,
        ema_fast=latest(ema(closes, EMA_FAST)),
        ema_slow=latest(ema(closes, EMA_SLOW)),
        rsi=latest(rsi(closes, RSI_PERIOD)),
        macd=momentum.macd if momentum else None,
        macd_signal=momentum.signal if momentum else None,
        macd_histogram=momentum.histogram if momentum else None,
        atr=latest(atr(bars, ATR_PERIOD)),
        average_volume=latest(volume_average(bars, VOLUME_PERIOD)),
        return_fraction=latest(returns(closes, RETURN_PERIOD)),
        observations=len(closes),
    )


async def ask_advisor(
    model_key: str | None, context: PortfolioAnalysisContext
) -> tuple[AnalysisModel, PortfolioAdvice]:
    """The ONE model call of a run: the whole account in, one answer out.

    Nothing is validated here and nothing is saved: a failure — an unknown
    model, a missing key, a refused call — becomes an :class:`AnalysisError`
    and the run stops without a proposal.
    """
    try:
        model = advice_service.resolve_model(model_key)
        advisor = advice_service.build_advisor(model)
        answer = await advisor.analyze(context)
        return model, answer
    except Exception as exc:  # noqa: BLE001 - every failure is one clear error
        raise AnalysisError(f"The analysis could not be completed: {exc}") from exc


async def analyse_portfolio(
    model_key: str | None,
    *,
    ledger: PaperLedger,
    evidence: dict[str, IndicatorEvidence],
    universe: UniverseConfig,
    warnings: Sequence[str] = (),
) -> tuple[AnalysisModel, PortfolioAnalysisContext, PortfolioAdvice]:
    """Build the analysis context and ask the model once."""
    context = advice_service.analysis_context(
        country=ledger.country,
        ledger=ledger,
        evidence=evidence,
        universe=universe,
        warnings=warnings,
    )
    model, answer = await ask_advisor(model_key, context)
    return model, context, answer


def validate_advice(
    advice: PortfolioAdvice,
    *,
    model: AnalysisModel,
    context: PortfolioAnalysisContext,
    universe: UniverseConfig,
) -> PortfolioAdviceView:
    """Check the target allocation against the account and flatten it.

    Refuses an answer that forgets a holding, invents a symbol, or allocates
    money the account does not have. A refusal is an :class:`AnalysisError`, so
    no proposal can record advice that did not fit the account.
    """
    try:
        return advice_service.validated_view(
            advice, model=model, context=context, universe=universe
        )
    except Exception as exc:  # noqa: BLE001 - every failure is one clear error
        raise AnalysisError(f"The advice was refused: {exc}") from exc


def prices_from_evidence(
    evidence: Mapping[str, IndicatorEvidence],
    ledger: PaperLedger,
) -> dict[str, Money]:
    """The usable prices for sizing a rebalance: evidence first, ledger next."""
    prices: dict[str, Money] = {}
    for symbol, reading in evidence.items():
        if reading.last_price is not None and reading.last_price.is_positive:
            prices[symbol] = reading.last_price
    for position in ledger.positions:
        if position.symbol not in prices and position.last_price.is_positive:
            prices[position.symbol] = position.last_price
    return prices


def build_rebalance_proposal(
    *,
    plan: rebalance_service.RebalancePlan,
    advice_view: PortfolioAdviceView,
    evidence: Mapping[str, IndicatorEvidence],
    warnings: Sequence[str] = (),
    model_key: str | None = None,
    attempts: int = 1,
    execution_mode: str = "LIVE",
) -> Proposal:
    """Assemble the immutable plan a human reviews from one rebalance.

    The proposal emphasises the model's target allocation and the exact
    simulated trades that implement it, and keeps both paper portfolios: the
    one copied from the broker at the start, and the one after the trades.
    """
    recommendations = rebalance_service.recommendations_from_rows(plan.rows, evidence)
    traded = money(0)
    for row in plan.trades:
        traded = traded + row.notional
    turnover = float(traded / plan.portfolio_value) if not plan.portfolio_value.is_zero else 0.0
    floor = plan.portfolio_value.amount * Decimal(str(advice_view.target_cash_weight))
    orders = [row for row in plan.trades]
    described = ", ".join(f"{row.side} {row.quantity} {row.symbol}" for row in orders)
    summary = advice_view.summary
    if described:
        summary = f"{summary} Planned: {described}."
    else:
        summary = f"{summary} No trade is needed; the allocation is kept."
    all_warnings = [*plan.warnings, *warnings]

    return Proposal(
        country=plan.ledger_before.country,
        currency=plan.ledger_before.currency,
        model_key=model_key,
        attempts=attempts,
        execution_mode=execution_mode,
        portfolio_value=plan.portfolio_value,
        cash=plan.ledger_before.cash,
        advice=advice_view,
        ledger_before=plan.ledger_before,
        ledger_after=plan.ledger_after,
        candidates=[],
        finalists=[],
        recommendations=recommendations,
        turnover=turnover,
        risk_checks=[
            RiskCheck(
                name="self_financing",
                passed=True,
                detail=(
                    "Sells are applied before buys and the rounding difference "
                    "stays in cash; no new capital is assumed."
                ),
            ),
            RiskCheck(
                name="cash_reserve",
                passed=plan.ledger_after.cash.amount >= floor,
                detail=(
                    f"The preview ends with {plan.ledger_after.cash} in cash "
                    f"against a target of {money(floor)}."
                ),
            ),
            RiskCheck(
                name="closed_portfolio",
                passed=True,
                detail=(
                    "The target allocation covers the whole portfolio and adds "
                    "up to 100% including cash."
                ),
            ),
        ],
        warnings=list(all_warnings),
        summary=summary,
        created_at=datetime.now(UTC),
    )


def _history_warnings(wanted: set[str], history: dict[str, list[PriceBar]]) -> list[str]:
    """What the model should know about instruments it has no data for."""
    return [
        f"No price history is saved for {symbol}."
        for symbol in sorted(wanted)
        if not history.get(symbol)
    ]


def history_symbols(universe: UniverseConfig, ledger: PaperLedger) -> set[str]:
    """Every symbol whose price history the plan needs: the universe plus held."""
    symbols = set(universe.symbols)
    symbols.update(position.symbol for position in ledger.positions)
    return symbols


def refresh_targets(universe: UniverseConfig, ledger: PaperLedger) -> list[tuple[str, str]]:
    """Every ``(symbol, market)`` pair to download history for."""
    targets = [(instrument.symbol, instrument.market) for instrument in universe.instruments]
    known = set(universe.symbols)
    for position in sorted(ledger.positions, key=lambda item: item.symbol):
        if position.symbol in known:
            continue
        targets.append((position.symbol, position.market or DEFAULT_MARKET))
    return targets


async def refresh_history(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    universe: UniverseConfig,
    ledger: PaperLedger,
) -> list[tuple[str, str]]:
    """Refresh price history for the universe and everything the ledger holds.

    Returns one ``(symbol, reason)`` pair per symbol that could not be read, so
    the caller can show the problem instead of letting it surface later as a
    missing indicator. A failure never stops the run: the symbol is judged on
    whatever history is saved.
    """
    failed: list[tuple[str, str]] = []
    for symbol, market in refresh_targets(universe, ledger):
        try:
            result = await broker_service.read_price_history(
                db,
                user_id=user_id,
                connection=connection,
                market=market,
                symbol=symbol,
            )
        except Exception:  # noqa: BLE001 - a bad market must not stop the run
            failed.append((symbol, "the broker read failed"))
            continue
        if not result.ok:
            failed.append((symbol, result.error or "the broker returned no data"))
    return failed


async def load_history(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    symbols: set[str],
) -> dict[str, list[PriceBar]]:
    """Read the saved price history for the given symbols."""
    history: dict[str, list[PriceBar]] = {}
    for symbol in sorted(symbols):
        snapshot = await snapshots_service.latest_snapshot(
            db,
            user_id=user_id,
            connection_id=connection.id,
            kind=price_history_kind(symbol),
        )
        if snapshot is None:
            continue
        history[symbol] = broker_service.bars_from_snapshot(snapshot)
    return history


# --- Saved proposals ------------------------------------------------------


async def save_proposal(
    db: AsyncSession,
    *,
    user_id: int,
    connection_id: int,
    proposal: Proposal,
) -> ProposalRecord:
    """Insert a proposal. There is no update path: a saved plan is final."""
    record = ProposalRecord(
        user_id=user_id,
        connection_id=connection_id,
        country=proposal.country,
        status=proposal.status,
        approval_mode=proposal.approval_mode,
        execution_mode=proposal.execution_mode,
        payload=proposal.model_dump(mode="json"),
        created_at=proposal.created_at or datetime.now(UTC),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    logger.info(
        "proposal id=%s saved for connection id=%s (%s orders, turnover=%.4f)",
        record.id,
        connection_id,
        len(proposal.orders),
        proposal.turnover,
    )
    return record


async def list_proposals(
    db: AsyncSession, *, user_id: int, connection_id: int, country: str
) -> list[ProposalRecord]:
    """Return a connection's proposals for one country, newest first."""
    result = await db.execute(
        select(ProposalRecord)
        .where(
            ProposalRecord.user_id == user_id,
            ProposalRecord.connection_id == connection_id,
            ProposalRecord.country == country,
        )
        .order_by(ProposalRecord.created_at.desc(), ProposalRecord.id.desc())
    )
    return list(result.scalars())


async def list_all_proposals(
    db: AsyncSession, *, user_id: int, limit: int = 200
) -> list[ProposalRecord]:
    """Return every proposal the user owns, newest first.

    Scoped to the signed-in user: one account never sees another account's
    plans, whatever connection or country they belong to.
    """
    result = await db.execute(
        select(ProposalRecord)
        .where(ProposalRecord.user_id == user_id)
        .order_by(ProposalRecord.created_at.desc(), ProposalRecord.id.desc())
        .limit(limit)
    )
    return list(result.scalars())


class ProposalDeleteError(ValueError):
    """The proposal cannot be deleted safely."""


#: Order states that must not be deleted: the broker may still act on them.
#: A partial fill is included: the rest of the order is still working.
UNSETTLED_ORDER_STATES = {
    "prepared",
    "submitted",
    "accepted",
    "partially_filled",
    "unknown",
}


async def delete_proposal(db: AsyncSession, *, user_id: int, proposal_id: int) -> ProposalRecord:
    """Delete one proposal and everything attached to it, safely.

    Refused while any order is unconfirmed or waiting at the broker
    (``prepared``/``submitted``/``accepted``/``unknown``): those rows are what a
    reconciliation would work from, and losing them could cause a duplicate
    live order. Everything else — the approval, settled orders, and the
    proposal's own events — goes in one transaction. One ``proposal.deleted``
    event is kept afterwards so the history still records that a plan existed
    and was removed.
    """
    record = await get_proposal(db, user_id=user_id, proposal_id=proposal_id)
    if record is None:
        raise ProposalDeleteError("Proposal not found.")

    orders = await orders_service.list_orders_for_proposal(
        db, user_id=user_id, proposal_id=proposal_id
    )
    stuck = [order for order in orders if order.status in UNSETTLED_ORDER_STATES]
    if stuck:
        raise ProposalDeleteError(
            "This proposal has orders that were sent to the broker and are not "
            f"settled yet ({stuck[0].symbol}: {stuck[0].status}). Reconcile "
            "them before deleting."
        )

    connection_id = record.connection_id
    summary = f"Proposal ID {record.id}"
    await events_service.delete_events_for_proposal(db, user_id=user_id, proposal_id=proposal_id)
    await orders_service.delete_orders_for_proposal(db, user_id=user_id, proposal_id=proposal_id)
    await approvals_service.delete_approvals_for_proposal(
        db, user_id=user_id, proposal_id=proposal_id
    )
    await db.delete(record)
    await db.commit()
    await events_service.record_event(
        db,
        user_id=user_id,
        connection_id=connection_id,
        category=EVENT_PROPOSAL,
        event_type="proposal.deleted",
        message=f"{summary} was deleted by its owner.",
        mode=None,
    )
    logger.info("proposal id=%s deleted by user id=%s", proposal_id, user_id)
    return record


async def get_proposal(
    db: AsyncSession, *, user_id: int, proposal_id: int
) -> ProposalRecord | None:
    """Return one proposal owned by the user, or None.

    The ``user_id`` filter is what keeps one user away from another user's plan;
    a mismatch behaves exactly like a missing row.
    """
    result = await db.execute(
        select(ProposalRecord).where(
            ProposalRecord.id == proposal_id,
            ProposalRecord.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def count_proposals(db: AsyncSession, *, user_id: int) -> int:
    """Return how many proposals the user has."""
    result = await db.execute(
        select(func.count()).select_from(ProposalRecord).where(ProposalRecord.user_id == user_id)
    )
    return int(result.scalar_one())
