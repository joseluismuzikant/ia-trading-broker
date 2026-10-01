"""The configured trading universe and the portfolio limits.

Every instrument the project considers is declared in one TOML file
(``config/universe.toml`` by default): symbol, paper name, and the category it
belongs to. Nothing else in the codebase names a ticker, so changing the
universe is a data edit rather than a code change.

The same file carries the portfolio constraints the risk manager enforces and
the widths of the analysis funnel. This module only parses and validates it;
it never reads the database, calls a broker, or knows what a proposal is.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

#: The categories the universe is split into, in the order they are shown.
CATEGORIES: tuple[str, ...] = ("argentina_stocks", "cedears", "bonds")

#: Market assumed for a category that does not name one.
DEFAULT_MARKET = "BCBA"

#: Where the shipped configuration lives, next to ``app`` and ``templates``.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "universe.toml"


class UniverseConfigError(ValueError):
    """The universe file is missing, unreadable, or internally inconsistent."""


@dataclass(frozen=True)
class Instrument:
    """One tradable instrument, exactly as the configuration declares it."""

    symbol: str
    #: The paper's name: the company, fund, or bond the ticker stands for.
    name: str
    category: str
    market: str = DEFAULT_MARKET


@dataclass(frozen=True)
class PortfolioConstraints:
    """The hard limits the risk manager enforces on every order."""

    #: Most positions the account may hold at once, across every category.
    max_open_positions: int = 8
    max_argentina_stock_positions: int = 4
    max_cedear_positions: int = 3
    max_bond_positions: int = 2
    #: Largest share of the portfolio one position may reach.
    max_single_position_pct: Decimal = Decimal("0.15")
    #: Smallest share of the portfolio that must stay in cash.
    min_cash_pct: Decimal = Decimal("0.10")
    #: Most orders one run may place. It is what bounds a run's turnover.
    max_trades_per_run: int = 3

    def max_positions_for(self, category: str) -> int:
        """How many positions of one category the account may hold at once."""
        caps = {
            "argentina_stocks": self.max_argentina_stock_positions,
            "cedears": self.max_cedear_positions,
            "bonds": self.max_bond_positions,
        }
        if category not in caps:
            raise UniverseConfigError(f"unknown category {category!r}")
        return caps[category]


@dataclass(frozen=True)
class SelectionSettings:
    """How wide each funnel stage is: the universe in, 10-12 candidates, 5 out."""

    candidates_min: int = 10
    candidates_max: int = 12
    finalists: int = 5


@dataclass(frozen=True)
class UniverseConfig:
    """The whole configured universe, with its limits and funnel widths."""

    instruments: tuple[Instrument, ...]
    constraints: PortfolioConstraints = PortfolioConstraints()
    selection: SelectionSettings = SelectionSettings()

    @property
    def symbols(self) -> tuple[str, ...]:
        """Every configured ticker, in configuration order."""
        return tuple(instrument.symbol for instrument in self.instruments)

    def for_category(self, category: str) -> list[Instrument]:
        """Every instrument of one category, in configuration order."""
        return [i for i in self.instruments if i.category == category]

    def find(self, symbol: str) -> Instrument | None:
        """The instrument behind one ticker, or None when it is not configured."""
        wanted = symbol.strip().upper()
        for instrument in self.instruments:
            if instrument.symbol == wanted:
                return instrument
        return None

    def category_of(self, symbol: str) -> str | None:
        """The category a ticker belongs to, or None when it is unknown."""
        instrument = self.find(symbol)
        return instrument.category if instrument else None


def load_universe_config(path: Path | str | None = None) -> UniverseConfig:
    """Parse the universe file, rejecting anything inconsistent.

    Validation is strict on purpose: a typo must stop the run with a clear
    message instead of silently dropping an instrument from the universe.
    """
    resolved = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UniverseConfigError(f"universe config not found: {resolved}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise UniverseConfigError(f"universe config is not valid TOML: {exc}") from exc

    instruments = _instruments_from(raw)
    constraints = _constraints_from(raw.get("portfolio_constraints"))
    selection = _selection_from(raw.get("selection"))
    return UniverseConfig(
        instruments=tuple(instruments),
        constraints=constraints,
        selection=selection,
    )


@lru_cache(maxsize=4)
def get_universe(path: str | None = None) -> UniverseConfig:
    """The universe as configured, parsed once and reused.

    ``path`` is the configuration file to read; ``None`` uses the shipped one.
    """
    return load_universe_config(path)


def _instruments_from(raw: dict) -> list[Instrument]:
    universe = raw.get("universe")
    if not isinstance(universe, dict) or not universe:
        raise UniverseConfigError("the [universe] section is missing or empty")

    unknown = sorted(set(universe) - set(CATEGORIES))
    if unknown:
        raise UniverseConfigError(
            f"unknown universe category {unknown[0]!r}; expected one of "
            + ", ".join(repr(category) for category in CATEGORIES)
        )

    instruments: list[Instrument] = []
    seen: dict[str, str] = {}
    for category in CATEGORIES:
        section = universe.get(category)
        if not isinstance(section, dict):
            raise UniverseConfigError(f"[universe.{category}] is missing")
        market = str(section.get("market") or DEFAULT_MARKET).strip().upper()
        rows = section.get("instruments")
        if not isinstance(rows, dict) or not rows:
            raise UniverseConfigError(
                f"[universe.{category}.instruments] has no instruments"
            )
        for symbol, name in rows.items():
            ticker = symbol.strip().upper()
            paper_name = str(name).strip()
            if not ticker:
                raise UniverseConfigError(
                    f"an instrument in [universe.{category}] has an empty symbol"
                )
            if not paper_name:
                raise UniverseConfigError(f"{ticker} has no paper name")
            previous = seen.get(ticker)
            if previous is not None:
                raise UniverseConfigError(
                    f"{ticker} is listed in both {previous} and {category}"
                )
            seen[ticker] = category
            instruments.append(
                Instrument(
                    symbol=ticker, name=paper_name, category=category, market=market
                )
            )
    return instruments


def _constraints_from(section: object) -> PortfolioConstraints:
    if not isinstance(section, dict):
        raise UniverseConfigError("the [portfolio_constraints] section is missing")
    required = (
        "max_open_positions",
        "max_argentina_stock_positions",
        "max_cedear_positions",
        "max_bond_positions",
        "max_single_position_pct",
        "min_cash_pct",
        "max_trades_per_run",
    )
    missing = [key for key in required if key not in section]
    if missing:
        raise UniverseConfigError(
            f"[portfolio_constraints] is missing {missing[0]}"
        )
    constraints = PortfolioConstraints(
        max_open_positions=_count(section["max_open_positions"], "max_open_positions"),
        max_argentina_stock_positions=_count(
            section["max_argentina_stock_positions"], "max_argentina_stock_positions"
        ),
        max_cedear_positions=_count(
            section["max_cedear_positions"], "max_cedear_positions"
        ),
        max_bond_positions=_count(section["max_bond_positions"], "max_bond_positions"),
        max_single_position_pct=_pct(
            section["max_single_position_pct"], "max_single_position_pct"
        ),
        min_cash_pct=_pct(section["min_cash_pct"], "min_cash_pct", allow_zero=True),
        max_trades_per_run=_count(
            section["max_trades_per_run"], "max_trades_per_run"
        ),
    )
    if constraints.min_cash_pct >= Decimal("1"):
        raise UniverseConfigError("min_cash_pct must stay below 1")
    return constraints


def _selection_from(section: object) -> SelectionSettings:
    if section is None:
        return SelectionSettings()
    if not isinstance(section, dict):
        raise UniverseConfigError("[selection] must be a table")
    settings = SelectionSettings(
        candidates_min=_count(section.get("candidates_min", 10), "candidates_min"),
        candidates_max=_count(section.get("candidates_max", 12), "candidates_max"),
        finalists=_count(section.get("finalists", 5), "finalists"),
    )
    if settings.candidates_min > settings.candidates_max:
        raise UniverseConfigError("candidates_min must not exceed candidates_max")
    return settings


def _count(value: object, label: str) -> int:
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise UniverseConfigError(f"{label} must be a whole number") from exc
    if count < 0:
        raise UniverseConfigError(f"{label} must not be negative")
    return count


def _pct(value: object, label: str, *, allow_zero: bool = False) -> Decimal:
    try:
        pct = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - any bad value is a config error
        raise UniverseConfigError(f"{label} must be a number") from exc
    if pct < 0 or (pct == 0 and not allow_zero) or pct > 1:
        raise UniverseConfigError(f"{label} must be a fraction between 0 and 1")
    return pct
