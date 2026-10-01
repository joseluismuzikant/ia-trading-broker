"""Market indicators, calculated in Python.

The README requires EMA, RSI, MACD, ATR, volume, returns, and portfolio
exposure to be computed here, before any optional analysis component is called.
Nothing in this module reaches a broker, a database, or a network. It takes
numbers and returns numbers.

Conventions:

* Inputs are ordered oldest first, which is how the broker returns them.
* A value is ``None`` until its window has enough observations. A short history
  never invents a number.
* Prices and volumes are plain floats. They describe the market; they are not
  money, so they do not go through :mod:`app.domain.money`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    """One period of market data, oldest to newest in a series."""

    close: float
    high: float | None = None
    low: float | None = None
    volume: float | None = None


@dataclass(frozen=True)
class MACDValue:
    """One MACD reading: the line, the signal line, and their difference."""

    macd: float
    signal: float
    histogram: float


def _validate_period(period: int) -> None:
    if period < 1:
        raise ValueError("period must be at least 1")


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average.

    The first value is the simple average of the first ``period`` observations,
    which is the standard seed. Earlier positions are ``None``.
    """
    _validate_period(period)
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result

    seed = sum(values[:period]) / period
    result[period - 1] = seed
    weight = 2.0 / (period + 1)
    previous = seed
    for index in range(period, len(values)):
        previous = values[index] * weight + previous * (1.0 - weight)
        result[index] = previous
    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """Relative Strength Index, Wilder smoothed, on a 0-100 scale.

    A series with no down moves reads 100, because there is no downward
    movement to measure.
    """
    _validate_period(period)
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result

    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    result[period] = _rsi_from_averages(average_gain, average_loss)

    for offset, (gain, loss) in enumerate(
        zip(gains[period:], losses[period:]), start=period + 1
    ):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
        result[offset] = _rsi_from_averages(average_gain, average_loss)
    return result


def _rsi_from_averages(average_gain: float, average_loss: float) -> float:
    if average_loss == 0.0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative_strength)


def macd(
    values: list[float],
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> list[MACDValue | None]:
    """Moving Average Convergence Divergence.

    The MACD line is the fast EMA minus the slow EMA. The signal line is an EMA
    of that difference, and the histogram is the gap between them.
    """
    if fast >= slow:
        raise ValueError("the fast period must be shorter than the slow period")
    _validate_period(signal)

    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    result: list[MACDValue | None] = [None] * len(values)

    macd_line = [
        None if fast_value is None or slow_value is None else fast_value - slow_value
        for fast_value, slow_value in zip(fast_ema, slow_ema)
    ]
    available = [value for value in macd_line if value is not None]
    signal_line = ema(available, signal)

    available_index = 0
    for index, value in enumerate(macd_line):
        if value is None:
            continue
        signal_value = signal_line[available_index]
        available_index += 1
        if signal_value is None:
            continue
        result[index] = MACDValue(
            macd=value, signal=signal_value, histogram=value - signal_value
        )
    return result


def true_ranges(bars: list[Bar]) -> list[float | None]:
    """True range for each bar.

    The first bar has no previous close, so its range is high minus low. A bar
    missing its high and low contributes nothing.
    """
    ranges: list[float | None] = []
    previous_close: float | None = None
    for bar in bars:
        if bar.high is None or bar.low is None:
            ranges.append(None)
        elif previous_close is None:
            ranges.append(bar.high - bar.low)
        else:
            ranges.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - previous_close),
                    abs(bar.low - previous_close),
                )
            )
        previous_close = bar.close
    return ranges


def atr(bars: list[Bar], period: int = 14) -> list[float | None]:
    """Average True Range, Wilder smoothed.

    Measures how much the price normally moves, not which way it is going.
    """
    _validate_period(period)
    ranges = true_ranges(bars)
    result: list[float | None] = [None] * len(bars)

    usable = [value for value in ranges if value is not None]
    if len(usable) < period:
        return result

    seed = sum(usable[:period]) / period
    seen = 0
    seed_index = 0
    for index, value in enumerate(ranges):
        if value is None:
            continue
        seen += 1
        if seen == period:
            seed_index = index
            break
    result[seed_index] = seed

    previous = seed
    for index in range(seed_index + 1, len(bars)):
        value = ranges[index]
        if value is None:
            continue
        previous = (previous * (period - 1) + value) / period
        result[index] = previous
    return result


def volume_average(bars: list[Bar], period: int = 20) -> list[float | None]:
    """Simple moving average of volume.

    Bars reporting no volume are skipped rather than treated as zero, because a
    missing volume is not the same as no trading.
    """
    _validate_period(period)
    result: list[float | None] = [None] * len(bars)
    window: list[float] = []
    for index, bar in enumerate(bars):
        if bar.volume is None:
            continue
        window.append(bar.volume)
        if len(window) > period:
            window.pop(0)
        if len(window) == period:
            result[index] = sum(window) / period
    return result


def returns(values: list[float], period: int = 1) -> list[float | None]:
    """Percentage return over ``period`` observations, as a fraction.

    ``0.05`` means a five percent gain. A zero starting price cannot produce a
    return, so it stays ``None``.
    """
    _validate_period(period)
    result: list[float | None] = [None] * len(values)
    for index in range(period, len(values)):
        start = values[index - period]
        if start == 0.0:
            continue
        result[index] = (values[index] - start) / start
    return result


def exposure(position_value: float, total_value: float) -> float | None:
    """Share of the portfolio invested in one position, as a fraction.

    Returns ``None`` when the portfolio value is unknown or zero, because an
    exposure cannot be measured against nothing.
    """
    if total_value <= 0.0 or math.isnan(total_value):
        return None
    return position_value / total_value


def latest(series: list) -> object:
    """The last computed value of a series, or ``None`` when none exists."""
    for value in reversed(series):
        if value is not None:
            return value
    return None
