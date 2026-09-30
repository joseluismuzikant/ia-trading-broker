"""Tests for the Python indicators."""

from __future__ import annotations

import pytest

from app.domain.indicators import (
    Bar,
    atr,
    ema,
    exposure,
    latest,
    macd,
    returns,
    rsi,
    true_ranges,
    volume_average,
)


# --- EMA ------------------------------------------------------------------


def test_ema_seeds_on_the_first_full_window_then_smooths() -> None:
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = ema(series, 3)

    assert result[:2] == [None, None]
    assert result[2] == pytest.approx(2.0)  # (1 + 2 + 3) / 3
    assert result[3] == pytest.approx(3.0)  # 4 * 0.5 + 2 * 0.5
    assert result[4] == pytest.approx(4.0)


def test_ema_of_a_short_series_is_all_none() -> None:
    assert ema([1.0, 2.0], 5) == [None, None]


def test_ema_rejects_a_non_positive_period() -> None:
    with pytest.raises(ValueError):
        ema([1.0], 0)


# --- RSI ------------------------------------------------------------------


def test_rsi_of_a_rising_series_is_one_hundred() -> None:
    series = [float(i) for i in range(1, 20)]
    result = rsi(series, 14)
    assert result[14] == pytest.approx(100.0)


def test_rsi_stays_within_bounds_and_needs_enough_history() -> None:
    series = [10.0, 11.0, 10.5, 12.0, 11.0, 13.0, 12.0, 14.0, 13.0, 15.0]
    result = rsi(series, 5)
    assert result[:5] == [None] * 5
    for value in result:
        if value is not None:
            assert 0.0 <= value <= 100.0


def test_rsi_rejects_a_non_positive_period() -> None:
    with pytest.raises(ValueError):
        rsi([1.0, 2.0], 0)


# --- MACD -----------------------------------------------------------------


def test_macd_histogram_is_the_gap_between_line_and_signal() -> None:
    series = [100 + i * 0.7 for i in range(60)]
    result = macd(series)
    reading = latest(result)
    assert reading is not None
    assert reading.histogram == pytest.approx(reading.macd - reading.signal)


def test_macd_is_none_until_the_signal_window_is_full() -> None:
    series = [100 + i for i in range(40)]
    result = macd(series)
    # The slow EMA starts at index 25 and the signal needs nine more values.
    assert all(value is None for value in result[:33])


def test_macd_rejects_a_fast_period_at_least_as_long_as_the_slow_one() -> None:
    with pytest.raises(ValueError):
        macd([1.0, 2.0, 3.0], fast=26, slow=12)


# --- ATR ------------------------------------------------------------------


def test_true_range_uses_high_minus_low_for_the_first_bar() -> None:
    bars = [Bar(close=10.0, high=12.0, low=9.0)]
    assert true_ranges(bars) == [3.0]


def test_true_range_accounts_for_a_gap_from_the_previous_close() -> None:
    bars = [
        Bar(close=10.0, high=11.0, low=9.0),
        Bar(close=15.0, high=16.0, low=14.5),
    ]
    ranges = true_ranges(bars)
    # The gap above the previous close (16 - 10) is larger than the bar range.
    assert ranges[1] == pytest.approx(6.0)


def test_a_bar_without_a_high_or_low_contributes_nothing() -> None:
    bars = [Bar(close=10.0), Bar(close=11.0)]
    assert true_ranges(bars) == [None, None]


def test_atr_needs_a_full_window_and_then_smooths() -> None:
    bars = [Bar(close=10.0 + i, high=11.0 + i, low=9.0 + i) for i in range(20)]
    result = atr(bars, 14)
    assert result[:13] == [None] * 13
    assert result[13] == pytest.approx(2.0)
    assert result[14] is not None


def test_atr_of_a_short_series_is_all_none() -> None:
    bars = [Bar(close=10.0, high=11.0, low=9.0)]
    assert atr(bars, 14) == [None]


# --- Volume ---------------------------------------------------------------


def test_volume_average_skips_missing_volumes() -> None:
    bars = [
        Bar(close=1.0, volume=10.0),
        Bar(close=1.0, volume=None),
        Bar(close=1.0, volume=20.0),
        Bar(close=1.0, volume=30.0),
    ]
    result = volume_average(bars, 3)
    # The window fills with 10, 20, 30 at the last bar.
    assert result[3] == pytest.approx(20.0)


def test_volume_average_needs_a_full_window() -> None:
    bars = [Bar(close=1.0, volume=10.0), Bar(close=1.0, volume=20.0)]
    assert volume_average(bars, 3) == [None, None]


# --- Returns --------------------------------------------------------------


def test_returns_are_a_fraction_of_the_starting_price() -> None:
    result = returns([100.0, 110.0, 121.0], 1)
    assert result[0] is None
    assert result[1] == pytest.approx(0.10)
    assert result[2] == pytest.approx(0.10)


def test_returns_over_several_periods_and_a_zero_start() -> None:
    result = returns([0.0, 50.0, 100.0], 1)
    # A zero starting price cannot produce a return.
    assert result[1] is None
    assert returns([10.0, 11.0, 12.0], 2)[2] == pytest.approx(0.2)


def test_returns_reject_a_non_positive_period() -> None:
    with pytest.raises(ValueError):
        returns([1.0], 0)


# --- Exposure -------------------------------------------------------------


def test_exposure_is_a_fraction_of_the_portfolio() -> None:
    assert exposure(25.0, 100.0) == pytest.approx(0.25)


def test_exposure_is_none_without_a_portfolio_value() -> None:
    assert exposure(25.0, 0.0) is None
    assert exposure(25.0, -5.0) is None


# --- Latest ---------------------------------------------------------------


def test_latest_returns_the_most_recent_computed_value() -> None:
    assert latest([None, 1.0, 2.0, None]) == 2.0
    assert latest([None, None]) is None
    assert latest([]) is None
