"""The prompt behind :class:`~app.infrastructure.openai.OpenAIPortfolioAdvisor`.

The version is part of the answer: a saved proposal records which prompt
produced it, so a change here is visible in the history rather than silent.

The prompt is the only place that speaks to the model. It exists to make one
thing unmistakable: the model is reshaping money that is already in the
account. Nothing in the application assumes new capital, so the model must not
either.
"""

from __future__ import annotations

import json

from app.domain.analysis import ALLOCATION_TOLERANCE, PortfolioAnalysisContext

#: Bump when the wording below changes in a way that can move an answer.
ANALYSIS_PROMPT_VERSION = "rebalance-v2"

#: Provenance the answer is stamped with; the model cannot choose it.
PROVIDER = "openai"

SYSTEM_PROMPT = f"""You are a portfolio analyst working on an existing \
portfolio.

You are modifying an existing portfolio. Do not assume the user adds capital. \
The current total portfolio value is the complete pool of capital available.

Return a complete target allocation.
Every existing holding must appear.
New positions may only come from the supplied watchlist.
Instrument target weights plus target cash must equal 100%.
Respect the minimum cash requirement.
Do not provide order quantities or broker requests.
Use only the supplied evidence.
Do not invent news, fundamentals, or unavailable data.

Rules for the answer:

- Positions exist only in whole units. "unit_cost" is the price of one \
tradable unit and "one_unit_weight" is its share of the total portfolio. A \
target weight must be 0 or at least "one_unit_weight", and a change smaller \
than "one_unit_weight" cannot be traded, so keep the current weight instead. \
Never target an instrument whose "one_unit_weight" is above \
"max_single_position_pct": it can never be bought. When "unit_cost" is null \
the price is unknown, so do not add the instrument.
- "target_weight" is the share of the total portfolio value the instrument \
should hold after the rebalance, between 0 and 1. Zero means a complete exit.
- "target_cash_weight" is the share that should stay in cash, between 0 and 1.
- The sum of all "target_weight" values plus "target_cash_weight" must be 1.0. \
No money is created and none is lost.
- "action" describes the move from the current weight to the target one: BUY \
when the target is more than {ALLOCATION_TOLERANCE} above the current weight, \
SELL when it is more than {ALLOCATION_TOLERANCE} below it, otherwise HOLD. A \
new position is a BUY, a full exit is a SELL.
- "confidence" is between 0 and 1 and is descriptive only.
- "rationale" is one short sentence. "risks" lists the known risks of the move.
- "model_provider" is "{PROVIDER}", "model_name" is the model that \
is answering, and "analysis_version" is "{ANALYSIS_PROMPT_VERSION}".
"""


def _last_price(context: PortfolioAnalysisContext, symbol: str) -> float | None:
    """The latest priced reading for a symbol, or None when there is none."""
    reading = context.evidence.get(symbol)
    if reading is None or reading.last_price is None:
        return None
    return float(reading.last_price.amount)


def _unit_terms(price: float | None, factor: int, total: float | None) -> dict:
    """What one whole unit costs, and how much of the portfolio that is.

    A quote may cover more than one unit (bonds are quoted per 100 nominales),
    so one unit costs ``price / factor``. The model uses it to avoid asking for
    a weight Python can never fill with whole units.
    """
    if not price or price <= 0 or not total or total <= 0:
        return {"unit_cost": None, "one_unit_weight": None}
    unit_cost = price / max(factor, 1)
    return {"unit_cost": round(unit_cost, 4), "one_unit_weight": round(unit_cost / total, 6)}


def user_message(context: PortfolioAnalysisContext) -> str:
    """One request describing the whole account.

    The payload is built from the analysis context only, which carries no
    credentials, no session, and no raw broker response: symbols, names,
    numbers, indicators, the watchlist, the limits, and the caller's warnings.
    """
    total = context.total_value
    factors = {
        instrument.symbol.strip().upper(): instrument.quotation_factor
        for instrument in context.watchlist
    }
    positions = []
    for position in context.portfolio.positions:
        symbol = position.instrument.symbol.strip().upper()
        if not symbol:
            continue
        positions.append(
            {
                "symbol": symbol,
                "name": position.instrument.description,
                "quantity": position.total_quantity,
                "average_price": position.average_price,
                "current_price": position.last_price,
                "market_value": position.market_value,
                "current_weight": round(context.weight_of(symbol), 6),
                **_unit_terms(position.last_price, factors.get(symbol, 1), total),
            }
        )

    evidence = {
        symbol: {
            "last_price": reading.last_price,
            "ema_fast": reading.ema_fast,
            "ema_slow": reading.ema_slow,
            "rsi": reading.rsi,
            "macd": reading.macd,
            "macd_signal": reading.macd_signal,
            "macd_histogram": reading.macd_histogram,
            "atr": reading.atr,
            "average_volume": reading.average_volume,
            "return_fraction": reading.return_fraction,
            "observations": reading.observations,
        }
        for symbol, reading in sorted(context.evidence.items())
    }

    constraints = context.constraints
    payload = {
        "country": context.country,
        "analysed_at": context.analysed_at.isoformat(),
        "portfolio": {
            "cash": context.cash,
            "cash_weight": context.cash_weight,
            "total_value": context.total_value,
            "positions": positions,
        },
        "evidence": evidence,
        "watchlist": [
            {
                "symbol": instrument.symbol,
                "name": instrument.name,
                "category": instrument.category,
                "market": instrument.market,
                **_unit_terms(
                    _last_price(context, instrument.symbol),
                    instrument.quotation_factor,
                    total,
                ),
            }
            for instrument in context.watchlist
        ],
        "constraints": {
            "max_open_positions": constraints.max_open_positions,
            "max_single_position_pct": float(constraints.max_single_position_pct),
            "min_cash_pct": float(constraints.min_cash_pct),
            "max_trades_per_run": constraints.max_trades_per_run,
        },
        "warnings": list(context.warnings),
    }
    return json.dumps(payload, indent=2, default=str)
