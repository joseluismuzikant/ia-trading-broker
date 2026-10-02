"""Tests for the display formatting used by the pages and the PDF export.

Money is read locally (``99.895,16 ARS``), timestamps are shown in local time
with their offset (``... UTC-3``), and anything without a value renders as an
em dash rather than as zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.money import money
from app.formatting import (
    currency_label,
    format_datetime,
    format_money,
    format_number,
    format_percent,
)


# --- Money ----------------------------------------------------------------


def test_money_uses_local_separators() -> None:
    assert format_money(money("99895.16")) == "99.895,16"
    assert format_money(Decimal("0.5")) == "0,50"
    assert format_money(1234.5) == "1.234,50"


def test_money_keeps_small_and_large_amounts_readable() -> None:
    assert format_money(money("0.01")) == "0,01"
    assert format_money(money("1234567.89")) == "1.234.567,89"
    assert format_money(money("-1234.56")) == "-1.234,56"


def test_money_appends_the_currency_code() -> None:
    assert format_money(money("99895.16"), "Peso_Argentino") == "99.895,16 ARS"
    assert format_money(money("10.00"), "US$") == "10,00 USD"
    assert format_money(money("10.00"), "ars") == "10,00 ARS"


def test_money_without_a_currency_has_no_code() -> None:
    assert format_money(money("10.00")) == "10,00"
    assert format_money(money("10.00"), None) == "10,00"
    assert format_money(money("10.00"), "") == "10,00"


def test_a_missing_amount_is_a_dash_and_not_zero() -> None:
    assert format_money(None) == "—"
    assert format_money(None, "ARS") == "—"
    assert format_money("") == "—"
    assert format_money("not a number") == "—"


def test_money_accepts_the_values_the_pages_hold() -> None:
    assert format_money(100, "ARS") == "100,00 ARS"
    assert format_money("1234.5", "ARS") == "1.234,50 ARS"
    assert format_money(money("10.00"), "ARS") == "10,00 ARS"


# --- Numbers and percentages ----------------------------------------------


def test_numbers_use_the_same_separators() -> None:
    assert format_number(12345.678) == "12.345,68"
    assert format_number(12345.6789, 3) == "12.345,679"
    assert format_number(12345.6, 0) == "12.346"
    assert format_number(None) == "—"


def test_percentages_are_fractions_with_a_sign() -> None:
    assert format_percent(0.15) == "15,00%"
    assert format_percent(1.2345) == "123,45%"
    assert format_percent(0.0) == "0,00%"
    assert format_percent(None) == "—"


# --- Currency labels ------------------------------------------------------


def test_currency_values_map_to_short_codes() -> None:
    assert currency_label("Peso_Argentino") == "ARS"
    assert currency_label("Dolar_Estadounidense") == "USD"
    assert currency_label("AR$") == "ARS"
    assert currency_label("US$") == "USD"
    assert currency_label("ars") == "ARS"
    assert currency_label("EUR") == "EUR"
    assert currency_label(None) == ""


# --- Timestamps -----------------------------------------------------------


def test_timestamps_are_shown_in_local_time_with_the_offset() -> None:
    # 14:42 UTC is 11:42 in Argentina (UTC-3).
    moment = datetime(2026, 10, 2, 14, 42, 6, tzinfo=timezone.utc)

    assert format_datetime(moment) == "2026-10-02 11:42:06 UTC-3"


def test_a_timestamp_without_a_timezone_is_read_as_utc() -> None:
    naive = datetime(2026, 10, 2, 14, 42, 6)

    assert format_datetime(naive) == "2026-10-02 11:42:06 UTC-3"


def test_a_timestamp_in_another_timezone_is_converted() -> None:
    plus_two = datetime(2026, 10, 2, 16, 42, 6, tzinfo=timezone(timedelta(hours=2)))

    assert format_datetime(plus_two) == "2026-10-02 11:42:06 UTC-3"


def test_a_missing_timestamp_is_a_dash() -> None:
    assert format_datetime(None) == "—"
    assert format_datetime("2026-10-02") == "—"
