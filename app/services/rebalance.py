"""Deterministic rebalance: one target allocation, exact simulated trades.

The model chooses *where the money should be*; this module alone decides *how
many units move*. It takes a validated :class:`~app.domain.analysis.PortfolioAdvice`,
the current :class:`~app.domain.trading.PaperLedger`, current prices, the
universe metadata, and the configured constraints, and returns the exact
BUY / SELL / HOLD rows plus the paper portfolio after applying them.

Everything here is deterministic and uses exact :class:`~app.domain.money.Money`:
never binary floats, never a second look at the model. The rules are:

* ``target_value = portfolio value * target_weight``; the delta against the
  current value decides BUY, SELL, or HOLD;
* a quote may cover more than one unit: stocks and CEDEARs quote per 1, but the
  sovereign bonds quote per 100 nominales, so one tradable unit costs
  ``quoted_price / quotation_factor`` and an order moves
  ``quoted_price * quantity / quotation_factor``. The price sent to IOL stays
  the quoted price;
* quantities are whole units rounded **down**, and the rounding difference
  simply stays in cash rather than inventing money;
* SELLS are planned and applied before BUYS, so sale proceeds fund the buys;
* never sell more than is held, never buy with money the ledger does not have,
  and keep the configured cash floor untouched;
* one position may not exceed ``max_single_position_pct``, the open-position
  and category caps are respected, and an instrument's minimum quantity is
  honoured; a trade the model wanted but Python cannot size is marked
  **NOT EXECUTABLE** with a reason instead of being faked;
* at most ``max_trades_per_run`` trades are planned per run;
* a hold that needs no transaction is a HOLD, never a warning.

The result is a preview: applying it touches only the simulated ledger, and the
rows it returns are the very orders a later approval executes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from app.domain.analysis import AssetAdvice, PortfolioAdvice
from app.domain.money import Money, money, quantity
from app.domain.trading import (
    Action,
    IndicatorEvidence,
    Order,
    OrderSide,
    PaperLedger,
    Recommendation,
    RiskCheck,
)
from app.domain.universe import PortfolioConstraints, UniverseConfig
from app.services import ledger as ledger_service

#: Whole units per trade. IOL's ``cantidad`` is a unit count, so a fraction is
#: never sent; the leftover value stays in cash.
UNIT = Decimal("1")


class RebalanceError(ValueError):
    """The allocation cannot be turned into a safe rebalance."""


@dataclass(frozen=True)
class RebalanceRow:
    """One symbol's place in the rebalance: the advice, and what would trade."""

    symbol: str
    name: str
    category: str
    market: str | None
    action: Action
    side: OrderSide | None
    quantity: Decimal
    price: Money
    notional: Money
    current_weight: float
    target_weight: float
    weight_after: float
    #: Whole units the target implies, and the smallest executable quantity.
    required_quantity: Decimal
    min_quantity: Decimal
    #: Cost of one tradable unit (quoted price / quotation factor).
    unit_cost: Money
    quotation_factor: int
    #: False when the model wanted a trade Python could not size; ``reason``
    #: then says why. HOLD rows are executable (nothing to do) and trades are
    #: executable by construction.
    executable: bool
    reason: str
    rationale: str
    risks: tuple[str, ...]
    risk_checks: tuple[RiskCheck, ...]

    @property
    def is_trade(self) -> bool:
        """True when this row moves units."""
        return self.side is not None and self.quantity > 0

    @property
    def is_hold(self) -> bool:
        """True when the row needs no transaction at all."""
        return self.action == "HOLD" and self.side is None

    @property
    def passed_risk(self) -> bool:
        """True when every check on this row passed."""
        return all(check.passed for check in self.risk_checks)


@dataclass(frozen=True)
class RebalancePlan:
    """The exact rebalance: rows, the paper preview, and what was left over."""

    rows: tuple[RebalanceRow, ...]
    ledger_before: PaperLedger
    ledger_after: PaperLedger
    warnings: tuple[str, ...]
    portfolio_value: Money
    current_cash_weight: float
    target_cash_weight: float

    @property
    def trades(self) -> list[RebalanceRow]:
        """The rows that move units, sells before buys, each group by symbol."""
        rank = {"SELL": 0, "BUY": 1}
        moving = [row for row in self.rows if row.is_trade]
        return sorted(moving, key=lambda row: (rank.get(row.side or "BUY", 2), row.symbol))

    @property
    def orders(self) -> list[RebalanceRow]:
        """Alias kept for readability at call sites: these become orders."""
        return self.trades

    @property
    def not_executable(self) -> list[RebalanceRow]:
        """Rows the model wanted to trade but which cannot be executed."""
        return [row for row in self.rows if not row.executable]


def _units(value: Decimal) -> Decimal:
    """Whole units that fit in ``value``, rounded down."""
    return quantity(value.to_integral_value(rounding=ROUND_DOWN))


def _usable_price(symbol: str, ledger: PaperLedger, prices: Mapping[str, Money]) -> Money | None:
    """The quoted price a trade may use.

    A held position is valued at the ledger's own mark, so a sale or a trim
    moves the same money the current value was computed from and the preview
    cannot lose or invent value. A brand-new symbol has no mark yet and uses
    the evidence price instead.
    """
    position = ledger.position_for(symbol)
    if position is not None and position.last_price.is_positive:
        return position.last_price
    price = prices.get(symbol)
    if price is not None and price.is_positive:
        return price
    return None


def plan_rebalance(
    *,
    advice: PortfolioAdvice,
    ledger: PaperLedger,
    universe: UniverseConfig,
    prices: Mapping[str, Money],
    constraints: PortfolioConstraints | None = None,
) -> RebalancePlan:
    """Turn one validated target allocation into the exact simulated trades.

    The advice must already have passed
    :func:`~app.services.advice_validation.validate_portfolio_advice`; this
    function does not re-argue with the model, it only sizes and applies.
    """
    limits = constraints if constraints is not None else universe.constraints
    total = ledger.total_value
    if not total.is_positive:
        raise RebalanceError("The portfolio has no value, so a target allocation cannot be sized.")
    total_amount = total.amount

    warnings: list[str] = []
    by_symbol = {instrument.symbol: instrument for instrument in universe.instruments}
    decisions: list[AssetAdvice] = sorted(advice.decisions, key=lambda item: item.symbol)

    # --- Size every decision before ordering trades ----------------------
    wanted: dict[str, dict] = {}
    for decision in decisions:
        instrument = by_symbol.get(decision.symbol)
        position = ledger.position_for(decision.symbol)
        price = _usable_price(decision.symbol, ledger, prices)
        if price is None:
            warnings.append(f"No usable price for {decision.symbol}; it is left unchanged.")
            continue

        # A quote may cover more than one unit (bonds quote per 100 nominales).
        factor = instrument.quotation_factor if instrument else 1
        unit_cost = money(price.amount / factor)
        min_units = Decimal(instrument.min_quantity) if instrument else Decimal("0")
        if min_units < UNIT:
            min_units = UNIT

        held_units = position.quantity if position is not None else Decimal("0")
        current_value = position.market_value if position is not None else money(0)
        target_value = money(total_amount * Decimal(str(decision.target_weight)))
        delta = target_value.amount - current_value.amount
        wanted[decision.symbol] = {
            "decision": decision,
            "instrument": instrument,
            "position": position,
            "price": price,
            "factor": factor,
            "unit_cost": unit_cost,
            "min_units": min_units,
            "held_units": held_units,
            "current_value": current_value,
            "target_value": target_value,
            "delta": delta,
            "units": Decimal("0"),
            "executable": True,
            "reason": "",
        }

    # --- Plan sells first: released cash may fund the buys ---------------
    sells: list[tuple[str, Decimal]] = []
    sell_proceeds = Decimal("0")
    trades_taken = 0
    for symbol, item in sorted(wanted.items(), key=lambda pair: (pair[1]["delta"], pair[0])):
        delta = item["delta"]
        if delta >= 0:
            continue
        item["required_quantity"] = _units((-delta) / item["unit_cost"].amount)
        if item["decision"].action == "HOLD":
            continue  # a hold needs no transaction
        if item["required_quantity"] < UNIT:
            # A trim smaller than one unit cannot be traded and is not a
            # failure: the position is already as close to the target as whole
            # units allow, so it is held.
            _within_one_unit(item)
            continue
        if not _live_quantity_ok(item):
            continue
        if trades_taken >= limits.max_trades_per_run:
            _block(item, f"trade limit ({limits.max_trades_per_run} per run) reached")
            continue

        sellable = _units(item["held_units"])
        units = min(item["required_quantity"], sellable)
        if units <= 0:
            _block(item, "the position holds less than one tradable unit")
            continue
        if units < item["min_units"]:
            _block(
                item,
                f"minimum order is {item['min_units']} unit(s)",
            )
            continue
        if units < item["required_quantity"]:
            warnings.append(
                f"{symbol} is sold down to the units actually held "
                f"({units} of {item['required_quantity']} wanted)."
            )
        item["units"] = units
        sells.append((symbol, units))
        sell_proceeds += _cash(item, units).amount
        trades_taken += 1

    # --- Plan buys against cash plus proceeds, keeping the cash floor ----
    floor_reserve = money(total_amount * limits.min_cash_pct).amount
    available = ledger.cash.amount + sell_proceeds - floor_reserve
    if available < 0:
        available = Decimal("0")
        warnings.append("The cash floor is already used up; no buy can be funded this run.")

    trades_taken = len(sells)
    buys: list[tuple[str, Decimal]] = []
    for symbol, item in sorted(wanted.items(), key=lambda pair: (-pair[1]["delta"], pair[0])):
        delta = item["delta"]
        if delta <= 0:
            continue
        unit_cost = item["unit_cost"]
        item["required_quantity"] = _units(delta / unit_cost.amount)
        if item["decision"].action == "HOLD":
            continue  # a hold needs no transaction
        if item["required_quantity"] < UNIT and item["held_units"] > 0:
            _within_one_unit(item)
            continue
        if item["required_quantity"] < UNIT:
            _block(
                item,
                f"the target needs {delta} but one tradable unit costs "
                f"{unit_cost}, so a whole unit does not fit",
            )
            continue
        if not _live_quantity_ok(item):
            continue

        # One position may never grow past its cap.
        cap_room = (limits.max_single_position_pct * total_amount) - (item["current_value"].amount)
        if cap_room < unit_cost.amount:
            _block(
                item,
                f"one unit costs {unit_cost} but the position cap "
                f"({float(limits.max_single_position_pct):.0%} of the portfolio) "
                "leaves less room than that",
            )
            continue
        wanted_units = _units(delta / unit_cost.amount)
        units = min(wanted_units, _units(cap_room / unit_cost.amount))
        if units < wanted_units:
            warnings.append(
                f"{symbol} is capped at the position limit "
                f"({float(limits.max_single_position_pct):.0%} of the portfolio)."
            )

        # Open-position and category caps only apply to brand-new holdings.
        if item["position"] is None:
            blocked = _position_cap_block(
                symbol,
                buys=buys,
                ledger=ledger,
                sells=sells,
                universe=universe,
                limits=limits,
            )
            if blocked:
                _block(item, blocked)
                continue

        cost = _cash(item, units)
        if cost.amount > available:
            units = _units(available / unit_cost.amount)
            if units <= 0:
                _block(item, "the available cash does not fund one tradable unit")
                continue
            warnings.append(
                f"{symbol} is bought with the cash available; the difference stays in cash."
            )

        if units < item["min_units"]:
            _block(item, f"minimum order is {item['min_units']} unit(s)")
            continue
        if trades_taken >= limits.max_trades_per_run:
            _block(item, f"trade limit ({limits.max_trades_per_run} per run) reached")
            continue

        available -= _cash(item, units).amount
        item["units"] = units
        buys.append((symbol, units))
        trades_taken += 1

    # --- Apply the plan to a copy of the ledger: sells first, then buys ---
    # Each group is applied in symbol order, which is exactly the order the
    # execution service later submits in.
    after = ledger
    for side, entries in (("SELL", sells), ("BUY", buys)):
        for symbol, units in sorted(entries):
            if units <= 0:
                continue
            item = wanted[symbol]
            order = _preview_order(item, side=side, units=units)
            after = ledger_service.apply_fill(
                after,
                order,
                filled_quantity=units,
                filled_price=item["price"],
            )

    rows = tuple(
        _row_for(symbol, item, ledger_before=ledger, ledger_after=after)
        for symbol, item in sorted(wanted.items())
    )
    return RebalancePlan(
        rows=rows,
        ledger_before=ledger,
        ledger_after=after,
        warnings=tuple(warnings),
        portfolio_value=total,
        current_cash_weight=float(ledger.cash / total),
        target_cash_weight=advice.target_cash_weight,
    )


def correction_notes(plan: RebalancePlan) -> list[str]:
    """Why each trade the model asked for cannot be filled, as feedback to it.

    A plan with no such rows needs no correction. Each note names the symbol,
    the weight the model asked for, and the planner's own reason, so the model
    can change that target instead of guessing.
    """
    return [
        f"{row.symbol}: a target of {row.target_weight:.1%} cannot be traded: {row.reason}"
        for row in plan.not_executable
    ]


def _within_one_unit(item: dict) -> None:
    """Record that a held position is within one unit of its target: a hold."""
    item["within_one_unit"] = True
    item["reason"] = (
        f"within one tradable unit ({item['unit_cost']}) of the target; no trade is needed"
    )


def _block(item: dict, reason: str) -> None:
    """Mark a row the model wanted to trade as not executable, with a reason."""
    item["units"] = Decimal("0")
    item["executable"] = False
    item["reason"] = reason


def _live_quantity_ok(item: dict) -> bool:
    """False (and blocks the row) when the API quantity field is unverified.

    A category whose ``live_quantity_confirmed`` is false cannot be traded
    live until the meaning of IOL's ``cantidad`` field is checked, so the plan
    marks the trade NOT EXECUTABLE instead of letting it be approved and then
    blocked at submission. Defence in depth: the live preflight checks this
    again.
    """
    instrument = item["instrument"]
    if instrument is None or instrument.live_quantity_confirmed:
        return True
    _block(
        item,
        f"the meaning of IOL's 'cantidad' field for {instrument.category} is "
        "unverified, so live orders are blocked. Verify it, then set "
        f"live_quantity_confirmed = true in [universe.{instrument.category}] "
        "of config/universe.toml.",
    )
    return False


def _cash(item: dict, units: Decimal) -> Money:
    """The money ``units`` of this instrument move, in the quote's convention."""
    return money(item["price"].amount * units / item["factor"])


def _position_cap_block(
    symbol: str,
    *,
    buys: list[tuple[str, Decimal]],
    ledger: PaperLedger,
    sells: list[tuple[str, Decimal]],
    universe: UniverseConfig,
    limits: PortfolioConstraints,
) -> str | None:
    """Why a brand-new position may not open, or None when it may."""
    fully_sold = {
        sold_symbol
        for sold_symbol, units in sells
        if ledger.position_for(sold_symbol) is not None
        and units >= ledger.position_for(sold_symbol).quantity
    }
    open_after = {
        position.symbol for position in ledger.positions if position.symbol not in fully_sold
    }
    open_after.update(bought_symbol for bought_symbol, _ in buys)
    if symbol not in open_after and len(open_after) >= limits.max_open_positions:
        return f"the account may hold at most {limits.max_open_positions} positions"

    instrument = universe.find(symbol)
    category = instrument.category if instrument else None
    if category is None:
        return None
    same_category = {
        other
        for other in open_after
        if (found := universe.find(other)) is not None and found.category == category
    }
    if symbol not in same_category:
        cap = limits.max_positions_for(category)
        if len(same_category) >= cap:
            return f"the {category} cap is {cap} positions"
    return None


def _preview_order(item: dict, *, side: OrderSide, units: Decimal) -> Order:
    """One fill-only order the paper ledger can apply. Never submitted."""
    return Order(
        symbol=item["decision"].symbol,
        side=side,
        quantity=units,
        limit_price=item["price"],
        notional=_cash(item, units),
        market=_market_for(item),
        quotation_factor=item["factor"],
        status="filled",
    )


def _row_for(
    symbol: str,
    item: dict,
    *,
    ledger_before: PaperLedger,
    ledger_after: PaperLedger,
) -> RebalanceRow:
    decision: AssetAdvice = item["decision"]
    price: Money = item["price"]
    units: Decimal = item.get("units", Decimal("0"))
    delta: Decimal = item["delta"]
    side: OrderSide | None = None
    if units > 0:
        side = "SELL" if delta < 0 else "BUY"
    action: Action = decision.action
    executable: bool = item.get("executable", True)
    if side is None and action != "HOLD":
        if item.get("within_one_unit"):
            # Whole units cannot get closer to the target: report it as a hold.
            action = "HOLD"
        else:
            # The model asked for a trade that Python could not size.
            executable = False

    position_after = ledger_after.position_for(symbol)
    weight_after = (
        float(position_after.market_value / ledger_after.total_value)
        if position_after is not None
        and not position_after.market_value.is_zero
        and ledger_after.total_value.is_positive
        else 0.0
    )

    checks: list[RiskCheck] = []
    if side is not None:
        checks.append(
            RiskCheck(
                name="funded",
                passed=True,
                detail="The trade is funded from cash and sale proceeds.",
            )
        )
        checks.append(
            RiskCheck(
                name="whole_units",
                passed=True,
                detail=f"{units} whole unit(s) at {price}.",
            )
        )
    return RebalanceRow(
        symbol=symbol,
        name=item["instrument"].name if item["instrument"] else "",
        category=item["instrument"].category if item["instrument"] else "",
        market=_market_for(item),
        action=action,
        side=side,
        quantity=units,
        price=price,
        notional=_cash(item, units),
        current_weight=float(item["current_value"] / ledger_before.total_value)
        if ledger_before.total_value.is_positive
        else 0.0,
        target_weight=decision.target_weight,
        weight_after=weight_after,
        required_quantity=item.get("required_quantity", units),
        min_quantity=item["min_units"],
        unit_cost=item["unit_cost"],
        quotation_factor=item["factor"],
        executable=executable,
        reason=item.get("reason", ""),
        rationale=decision.rationale,
        risks=tuple(decision.risks),
        risk_checks=tuple(checks),
    )


def _market_for(item: dict) -> str | None:
    position = item.get("position")
    if position is not None and position.market:
        return position.market
    instrument = item.get("instrument")
    if instrument is not None:
        return instrument.market
    return None


def recommendations_from_rows(
    rows: Sequence[RebalanceRow],
    evidence: Mapping[str, IndicatorEvidence] | None = None,
) -> list[Recommendation]:
    """The proposal's recommendation rows.

    Trades come first, sells before buys, so the stored plan and the order
    service see the same deterministic sequence. Not-executable rows and holds
    follow, so the review page can show the whole allocation.
    """
    readings = evidence or {}
    rank = {"SELL": 0, "BUY": 1}

    def key(row: RebalanceRow) -> tuple[int, str]:
        return (rank.get(row.side or "BUY", 2) if row.is_trade else 2, row.symbol)

    return [
        Recommendation(
            symbol=row.symbol,
            name=row.name,
            action=row.action,
            quantity=row.quantity,
            limit_price=row.price,
            notional=row.notional,
            weight_after=row.weight_after,
            target_weight=row.target_weight,
            market=row.market,
            executable=row.executable,
            reason=row.reason,
            required_quantity=row.required_quantity,
            min_quantity=row.min_quantity,
            unit_cost=row.unit_cost,
            quotation_factor=row.quotation_factor,
            rationale=row.rationale,
            evidence=readings.get(row.symbol, IndicatorEvidence()),
            risk_checks=list(row.risk_checks),
        )
        for row in sorted(rows, key=key)
    ]
