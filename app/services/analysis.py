"""Manual analysis: the funnel, the strategy, and a saved proposal.

One call reads the paper ledger and the saved price history, runs the analysis
funnel over the configured universe, then the fixed strategy and the fixed
policy, and stores the result as a proposal. The proposal is inserted once and
never updated, so the plan the review page shows cannot drift from the plan that
was checked.

The funnel is staged so the expensive stages stay small. The universe holds the
instruments declared in ``config/universe.toml`` (34 of them today). The market
scanner cuts that to a dozen candidates on cheap price-history readings, the
technical stage scores those, and the fundamental/news stage cuts them to a
handful of finalists. The risk manager then turns at most a few of those into
orders under the portfolio constraints.

Only the history refresh costs IOL calls: one price-history read per configured
instrument. Everything after it runs on the saved snapshots, which keeps the
analysis repeatable and free.

This service does not place an order.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain import portfolio_selection as selection
from app.domain.fundamentals import FundamentalView
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
from app.domain.money import money
from app.domain.policy import PlanState, advance, plan_risk_checks, size_and_check
from app.domain.trading import (
    CandidateView,
    FinalistView,
    IndicatorEvidence,
    PaperLedger,
    Proposal,
    Recommendation,
)
from app.domain.universe import DEFAULT_MARKET, UniverseConfig, get_universe
from app.infrastructure.deterministic import DeterministicFundamentals
from app.infrastructure.iol.schemas import Quote
from app.models import BrokerConnection, ProposalRecord, price_history_kind
from app.ports.fundamentals import FundamentalAnalyzer
from app.services import broker as broker_service
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


def load_config() -> UniverseConfig:
    """The configured universe and portfolio limits, from the settings' path."""
    path = get_settings().universe_config_path or None
    return get_universe(path)


def fundamental_analyser() -> FundamentalAnalyzer:
    """The outside source the finalists stage ranks on.

    The deterministic fallback needs no key and no network, so the funnel runs
    end to end today. When a news or language-model adapter is configured it
    replaces this function and nothing else changes.
    """
    return DeterministicFundamentals()


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


# --- The funnel -----------------------------------------------------------


def candidates_for(
    universe: UniverseConfig, evidence: dict[str, IndicatorEvidence]
) -> list[selection.Candidate]:
    """Funnel stage one: the configured universe, cut to its best candidates."""
    candidates = selection.scan_market(
        universe.instruments,
        evidence,
        limit=universe.selection.candidates_max,
    )
    if len(candidates) < universe.selection.candidates_min:
        logger.warning(
            "market scanner kept only %d of the %d candidates the funnel expects",
            len(candidates),
            universe.selection.candidates_min,
        )
    return candidates


async def outside_views(
    candidates: list[selection.Candidate],
) -> dict[str, FundamentalView]:
    """Funnel stage three's input: one outside view per candidate.

    One instrument failing does not stop the funnel: it is ranked on its
    technical score alone, and the failure is logged.
    """
    analyser = fundamental_analyser()
    views: dict[str, FundamentalView] = {}
    for candidate in candidates:
        try:
            views[candidate.symbol] = await analyser.analyse(
                candidate.instrument, candidate.evidence
            )
        except Exception:  # noqa: BLE001 - one bad source must not kill a run
            logger.warning(
                "fundamental analysis failed for %s", candidate.symbol, exc_info=True
            )
    return views


async def build_plan(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    ledger: PaperLedger,
) -> Proposal:
    """Run the whole funnel and the policy on saved data, spending no calls.

    History is read from the snapshots the refresh step saved, so a resumed run
    replays exactly the same inputs instead of a newer market.
    """
    universe = load_config()
    wanted = history_symbols(universe, ledger)
    history = await load_history(
        db, user_id=user_id, connection=connection, symbols=wanted
    )
    evidence = {
        symbol: evidence_for(history.get(symbol, [])) for symbol in sorted(wanted)
    }
    candidates = candidates_for(universe, evidence)
    views = await outside_views(candidates)
    finalists = selection.select_finalists(
        candidates, views, limit=universe.selection.finalists
    )
    logger.info(
        "funnel universe=%d candidates=%d finalists=%d",
        len(universe.instruments),
        len(candidates),
        len(finalists),
    )
    return build_proposal(
        ledger=ledger,
        evidence=evidence,
        candidates=candidates,
        finalists=finalists,
        universe=universe,
    )


def build_proposal(
    *,
    ledger: PaperLedger,
    evidence: dict[str, IndicatorEvidence],
    finalists: list[selection.Finalist],
    universe: UniverseConfig,
    candidates: Sequence[selection.Candidate] | None = None,
) -> Proposal:
    """Run the strategy and the policy over one ledger and its funnel result.

    Finalists are judged first and in rank order, so they get the run's limited
    orders before anyone else. Instruments the ledger holds that are not
    finalists are judged too, so an exit stays possible and a refused buy is
    visible rather than silent.
    """
    constraints = universe.constraints
    # The scanner's own output is stored with the plan so the review page can
    # show what was considered. A caller that only has finalists is understood
    # to have considered those alone.
    scanned = (
        list(candidates)
        if candidates is not None
        else [row.candidate for row in finalists]
    )
    recommendations: list[Recommendation] = []
    state = PlanState()

    for row in finalists:
        recommendation = size_and_check(
            symbol=row.symbol,
            name=row.name,
            evidence=row.evidence,
            ledger=ledger,
            constraints=constraints,
            category=row.category,
            is_finalist=True,
            state=state,
        )
        recommendations.append(recommendation)
        state = advance(state, recommendation, category=row.category)

    finalists_seen = {row.symbol for row in finalists}
    for position in sorted(ledger.positions, key=lambda item: item.symbol):
        if position.symbol in finalists_seen:
            continue
        instrument = universe.find(position.symbol)
        recommendation = size_and_check(
            symbol=position.symbol,
            name=instrument.name if instrument else "",
            evidence=evidence.get(position.symbol, IndicatorEvidence()),
            ledger=ledger,
            constraints=constraints,
            category=instrument.category if instrument else None,
            is_finalist=False,
            state=state,
        )
        recommendations.append(recommendation)
        state = advance(
            state,
            recommendation,
            category=instrument.category if instrument else None,
        )

    traded = state.traded
    turnover = (
        float(traded / ledger.total_value) if not ledger.total_value.is_zero else 0.0
    )
    return Proposal(
        country=ledger.country,
        currency=ledger.currency,
        portfolio_value=ledger.total_value,
        cash=ledger.cash,
        candidates=[
            CandidateView(
                symbol=row.symbol,
                name=row.name,
                category=row.category,
                scan_score=row.scan_score,
            )
            for row in scanned
        ],
        finalists=[
            FinalistView(
                symbol=row.symbol,
                name=row.name,
                category=row.category,
                technical_score=row.technical_score,
                fundamental_score=row.fundamental_score,
                fundamental_summary=row.fundamental_summary,
                combined_score=row.combined_score,
            )
            for row in finalists
        ],
        recommendations=recommendations,
        turnover=turnover,
        risk_checks=plan_risk_checks(
            state, ledger=ledger, constraints=constraints
        ),
        summary=_summarise(
            recommendations,
            funnel=(
                f"Funnel: {len(universe.instruments)} instruments "
                f"-> {len(scanned)} candidates -> {len(finalists)} finalists."
            ),
        ),
        created_at=datetime.now(UTC),
    )


def history_symbols(universe: UniverseConfig, ledger: PaperLedger) -> set[str]:
    """Every symbol whose price history the plan needs: the universe plus held."""
    symbols = set(universe.symbols)
    symbols.update(position.symbol for position in ledger.positions)
    return symbols


def refresh_targets(
    universe: UniverseConfig, ledger: PaperLedger
) -> list[tuple[str, str]]:
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


async def run_analysis(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    country: str,
) -> ProposalRecord:
    """Refresh the history, run the funnel, and store the resulting proposal.

    Raises :class:`AnalysisError` when there is no paper ledger yet, because a
    proposal must be sized against a known account.
    """
    ledger = await ledger_service.load_ledger(
        db, user_id=user_id, connection=connection, country=country
    )
    if ledger is None:
        raise AnalysisError(
            "No paper ledger exists for this country yet. Create one first."
        )
    universe = load_config()
    failed = await refresh_history(
        db,
        user_id=user_id,
        connection=connection,
        universe=universe,
        ledger=ledger,
    )
    if failed:
        logger.warning(
            "history refresh failed for %s: %s",
            len(failed),
            ", ".join(symbol for symbol, _ in failed),
        )
    proposal = await build_plan(
        db, user_id=user_id, connection=connection, ledger=ledger
    )
    return await save_proposal(
        db, user_id=user_id, connection_id=connection.id, proposal=proposal
    )


# --- Presentation ---------------------------------------------------------


def _summarise(recommendations: list[Recommendation], *, funnel: str) -> str:
    """One sentence describing the plan."""
    parts: list[str] = [funnel]

    orders = [item for item in recommendations if item.is_order]
    blocked = [
        item for item in recommendations if item.action != "HOLD" and not item.passed_risk
    ]
    if not orders and not blocked:
        parts.append("No trade proposed. Every instrument is a hold.")
        return " ".join(parts)
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
    for symbol in sorted(symbols):
        snapshot = await snapshots_service.latest_snapshot(
            db,
            user_id=user_id,
            connection_id=connection.id,
            kind=price_history_kind(symbol),
        )
        if snapshot is None:
            continue
        history[symbol] = broker_service.quotes_from_snapshot(snapshot)
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
