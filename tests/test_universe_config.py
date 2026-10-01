"""Tests for the universe configuration loader.

The file is the only place instruments are named, so these tests pin the shape
that the rest of the pipeline relies on: three categories with paper names, and
the portfolio limits the risk manager enforces.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.universe import (
    CATEGORIES,
    DEFAULT_CONFIG_PATH,
    SelectionSettings,
    UniverseConfigError,
    get_universe,
    load_universe_config,
)

# The universe the project is meant to trade, as the user fixed it.
ARGENTINA_STOCKS = [
    "YPFD", "VIST", "PAMP", "CEPU", "TGSU2", "TGNO4", "EDN", "TRAN", "GGAL",
    "BMA", "BBAR", "SUPV", "BYMA", "TXAR", "ALUA", "LOMA", "CRES", "COME",
    "MIRG", "METR",
]
CEDEARS = [
    "SPY", "QQQ", "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "MELI",
]
BONDS = ["AL30", "GD30", "AL35", "GD35"]


# --- The shipped configuration -------------------------------------------


def test_the_shipped_universe_holds_exactly_the_expected_instruments() -> None:
    config = load_universe_config()

    assert config.symbols == tuple(ARGENTINA_STOCKS + CEDEARS + BONDS)
    assert [i.symbol for i in config.for_category("argentina_stocks")] == ARGENTINA_STOCKS
    assert [i.symbol for i in config.for_category("cedears")] == CEDEARS
    assert [i.symbol for i in config.for_category("bonds")] == BONDS


def test_every_instrument_carries_its_paper_name() -> None:
    config = load_universe_config()

    assert len(config.instruments) == 34
    for instrument in config.instruments:
        assert instrument.name, f"{instrument.symbol} has no paper name"
    assert config.find("YPFD").name == "YPF S.A."
    assert config.find("al30").name == "Bonar 2030"
    assert config.find("MELI").name == "MercadoLibre, Inc."


def test_the_categories_are_the_three_the_plan_knows() -> None:
    assert CATEGORIES == ("argentina_stocks", "cedears", "bonds")

    config = load_universe_config()
    assert config.category_of("GGAL") == "argentina_stocks"
    assert config.category_of("AAPL") == "cedears"
    assert config.category_of("GD35") == "bonds"
    assert config.category_of("NOPE") is None


def test_the_portfolio_constraints_match_the_portfolio_rules() -> None:
    constraints = load_universe_config().constraints

    assert constraints.max_open_positions == 8
    assert constraints.max_argentina_stock_positions == 4
    assert constraints.max_cedear_positions == 3
    assert constraints.max_bond_positions == 2
    assert constraints.max_single_position_pct == Decimal("0.15")
    assert constraints.min_cash_pct == Decimal("0.10")
    assert constraints.max_trades_per_run == 3


def test_a_category_cap_is_read_by_category() -> None:
    constraints = load_universe_config().constraints

    assert constraints.max_positions_for("argentina_stocks") == 4
    assert constraints.max_positions_for("cedears") == 3
    assert constraints.max_positions_for("bonds") == 2
    with pytest.raises(UniverseConfigError):
        constraints.max_positions_for("crypto")


def test_the_selection_settings_deliver_the_funnel_widths() -> None:
    selection = load_universe_config().selection

    assert selection.candidates_min == 10
    assert selection.candidates_max == 12
    assert selection.finalists == 5


def test_the_universe_is_parsed_once_and_reused() -> None:
    assert get_universe() is get_universe()
    assert get_universe(str(DEFAULT_CONFIG_PATH)) is get_universe(str(DEFAULT_CONFIG_PATH))


# --- Validation -----------------------------------------------------------


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "universe.toml"
    path.write_text(body, encoding="utf-8")
    return path


MINIMAL = """
[universe.argentina_stocks.market]
"""

GOOD = """
[universe.argentina_stocks]
market = "BCBA"
[universe.argentina_stocks.instruments]
GGAL = "Grupo Financiero Galicia S.A."

[universe.cedears]
market = "BCBA"
[universe.cedears.instruments]
AAPL = "Apple Inc."

[universe.bonds]
market = "BCBA"
[universe.bonds.instruments]
AL30 = "Bonar 2030"

[portfolio_constraints]
max_open_positions = 8
max_argentina_stock_positions = 4
max_cedear_positions = 3
max_bond_positions = 2
max_single_position_pct = 0.15
min_cash_pct = 0.10
max_trades_per_run = 1

[selection]
candidates_min = 1
candidates_max = 2
finalists = 1
"""


def test_a_well_formed_file_loads(tmp_path: Path) -> None:
    config = load_universe_config(write_config(tmp_path, GOOD))

    assert [i.symbol for i in config.instruments] == ["GGAL", "AAPL", "AL30"]
    assert [i.name for i in config.instruments] == [
        "Grupo Financiero Galicia S.A.",
        "Apple Inc.",
        "Bonar 2030",
    ]
    assert config.constraints.max_trades_per_run == 1


def test_a_category_may_default_its_market(tmp_path: Path) -> None:
    body = GOOD.replace('market = "BCBA"\n', "")
    config = load_universe_config(write_config(tmp_path, body))

    assert {i.market for i in config.instruments} == {"BCBA"}


def test_a_missing_file_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(UniverseConfigError, match="not found"):
        load_universe_config(tmp_path / "nope.toml")


def test_invalid_toml_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(UniverseConfigError, match="not valid TOML"):
        load_universe_config(write_config(tmp_path, "[unclosed"))


def test_an_unknown_category_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace("[universe.bonds]", "[universe.crypto]")
    with pytest.raises(UniverseConfigError, match="crypto"):
        load_universe_config(write_config(tmp_path, body))


def test_a_missing_category_is_rejected(tmp_path: Path) -> None:
    body = GOOD.split("[universe.cedears]")[0]
    with pytest.raises(UniverseConfigError, match="universe.cedears"):
        load_universe_config(write_config(tmp_path, body))


def test_an_empty_category_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace('AAPL = "Apple Inc."', "")
    with pytest.raises(UniverseConfigError, match="cedears.instruments"):
        load_universe_config(write_config(tmp_path, body))


def test_a_symbol_without_a_paper_name_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace('GGAL = "Grupo Financiero Galicia S.A."', 'GGAL = ""')
    with pytest.raises(UniverseConfigError, match="GGAL has no paper name"):
        load_universe_config(write_config(tmp_path, body))


def test_a_symbol_listed_twice_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace('AL30 = "Bonar 2030"', 'GGAL = "Bonar 2030"')
    with pytest.raises(UniverseConfigError, match="GGAL is listed in both"):
        load_universe_config(write_config(tmp_path, body))


def test_a_missing_constraint_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace("min_cash_pct = 0.10", "")
    with pytest.raises(UniverseConfigError, match="min_cash_pct"):
        load_universe_config(write_config(tmp_path, body))


def test_a_percentage_out_of_range_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace("max_single_position_pct = 0.15", "max_single_position_pct = 15")
    with pytest.raises(UniverseConfigError, match="max_single_position_pct"):
        load_universe_config(write_config(tmp_path, body))


def test_a_negative_cap_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace("max_open_positions = 8", "max_open_positions = -1")
    with pytest.raises(UniverseConfigError, match="max_open_positions"):
        load_universe_config(write_config(tmp_path, body))


def test_a_funnel_that_narrows_nothing_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace("candidates_min = 1", "candidates_min = 3")
    with pytest.raises(UniverseConfigError, match="candidates_min"):
        load_universe_config(write_config(tmp_path, body))


def test_the_funnel_widths_may_be_left_out(tmp_path: Path) -> None:
    body = GOOD.split("[selection]")[0]
    selection = load_universe_config(write_config(tmp_path, body)).selection

    assert selection == SelectionSettings()
