"""Manual analysis: indicators, strategy, sizing, risk, and a saved proposal.

One call reads the paper ledger and the saved price history, runs the fixed
strategy and the fixed policy, and stores the result as a proposal. The proposal
is inserted once and never updated, so the plan the review page shows cannot
drift from the plan that was checked.

This service does not place an order, and it does not call the broker. Price
history is read from the snapshot saved by the refresh step, which keeps the
analysis free of charge and repeatable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.domain.policy import DEFAULT_ALLOWLIST, size_and_check
from app.domain.trading import (
    MAX_TURNOVER,
    IndicatorEvidence,
    PaperLedger,
    Proposal,
    Recommendation,
    RiskCheck,
)
from app.infrastructure.iol.schemas import Quote
from app.models import BrokerConnection, ProposalRecord, price_history_kind
from app.services import ledger as ledger_service
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


def evidence_for(quotes: list[Quote]) -> IndicatorEvidence:
    """Compute the indicator readings for one instrument's price history.

    Quotes arrive newest-first or oldest-first depending on the broker, so they
    are ordered by their timestamp before anything is calculated. A history that
    is too short produces readings of ``None``, which the strategy treats as "not
    enough evidence" rather than as a trade.
    """
    ordered = sorted(quotes, key=lambda quote: quote.fecha_hora or "")
    closes = [quote.ultimo_precio for quote in ordered if quote.ultimo_precio]
    bars = [
        Bar(
            close=quote.ultimo_precio,
            high=quote.maximo if quote.maximo is not None else quote.ultimo_precio,
            low=quote.minimo if quote.minimo is not None else quote.ultimo_precio,
            volume=quote.volumen_nominal,
        )
        for quote in ordered
        if quote.ultimo_precio
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


def build_proposal(
    *,
    ledger: PaperLedger,
    history: dict[str, list[Quote]],
    allowlist: frozenset[str] = DEFAULT_ALLOWLIST,
) -> Proposal:
    """Run the strategy and the policy over one ledger and its price history.

    Every allowlisted symbol is considered, whether or not the ledger holds it,
    so a buy can open a new position. Symbols the ledger holds that are not on
    the allowlist are considered too, so a blocked sell is visible, not silent.
    """
    symbols = _symbols_to_consider(ledger, history, allowlist)
    recommendations: list[Recommendation] = []
    spent = Money(Decimal("0.00"))

    for symbol in symbols:
        evidence = evidence_for(history.get(symbol, []))
        recommendation = size_and_check(
            symbol=symbol,
            evidence=evidence,
            ledger=ledger,
            allowlist=allowlist,
            spent=spent,
        )
        recommendations.append(recommendation)
        if recommendation.notional is not None:
            spent = spent + recommendation.notional

    turnover = (
        float(spent / ledger.total_value) if not ledger.total_value.is_zero else 0.0
    )
    return Proposal(
        country=ledger.country,
        currency=ledger.currency,
        portfolio_value=ledger.total_value,
        cash=ledger.cash,
        recommendations=recommendations,
        turnover=turnover,
        risk_checks=[_turnover_check(turnover)],
        summary=_summarise(recommendations),
        created_at=datetime.now(UTC),
    )


def _symbols_to_consider(
    ledger: PaperLedger,
    history: dict[str, list[Quote]],
    allowlist: frozenset[str],
) -> list[str]:
    """The instruments the analysis should judge, in a stable order."""
    held = {position.symbol for position in ledger.positions}
    return sorted(held | (allowlist & set(history)))


def _turnover_check(turnover: float) -> RiskCheck:
    limit = float(MAX_TURNOVER)
    passed = turnover <= limit + 1e-9
    return RiskCheck(
        name="plan_turnover",
        passed=passed,
        detail=(
            f"The plan trades {turnover:.2%} of the portfolio, "
            f"within the {limit:.0%} limit."
            if passed
            else (
                f"The plan trades {turnover:.2%} of the portfolio, "
                f"over the {limit:.0%} limit."
            )
        ),
    )


def _summarise(recommendations: list[Recommendation]) -> str:
    """One sentence describing the plan."""
    orders = [item for item in recommendations if item.is_order]
    blocked = [
        item for item in recommendations if item.action != "HOLD" and not item.passed_risk
    ]
    if not orders and not blocked:
        return "No trade proposed. Every instrument is a hold."
    parts: list[str] = []
    if orders:
        described = ", ".join(
            f"{item.action} {item.quantity} {item.symbol}" for item in orders
        )
        parts.append(f"Proposed: {described}.")
    if blocked:
        described = ", ".join(item.symbol for item in blocked)
        parts.append(f"Blocked by risk checks: {described}.")
    return " ".join(parts)


async def load_history(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    symbols: set[str],
) -> dict[str, list[Quote]]:
    """Read the saved price history for the given symbols."""
    history: dict[str, list[Quote]] = {}
    for symbol in symbols:
        snapshot = await snapshots_service.latest_snapshot(
            db,
            user_id=user_id,
            connection_id=connection.id,
            kind=price_history_kind(symbol),
        )
        if snapshot is None:
            continue
        payload = snapshot.payload.get("quotes", [])
        history[symbol] = [Quote.model_validate(item) for item in payload]
    return history


async def run_analysis(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    country: str,
    allowlist: frozenset[str] = DEFAULT_ALLOWLIST,
) -> ProposalRecord:
    """Build a proposal from the saved ledger and price history, and store it.

    Raises :class:`AnalysisError` when there is no paper ledger yet, because a
    proposal must be sized against a known account.
    """
    ledger = await ledger_service.load_ledger(
        db, user_id=user_id, connection=connection, country=country
    )
    if ledger is None:
        raise AnalysisError(
            "No paper ledger exists for this country yet. Create one from the "
            "portfolio before running an analysis."
        )

    symbols = {position.symbol for position in ledger.positions} | set(allowlist)
    history = await load_history(
        db, user_id=user_id, connection=connection, symbols=symbols
    )
    proposal = build_proposal(ledger=ledger, history=history, allowlist=allowlist)
    return await save_proposal(
        db, user_id=user_id, connection_id=connection.id, proposal=proposal
    )


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
        select(func.count())
        .select_from(ProposalRecord)
        .where(ProposalRecord.user_id == user_id)
    )
    return int(result.scalar_one())
