"""Tests for the analysis funnel: scan, technical score, and finalists.

These are the three pure stages between the configured universe and the short
list the risk manager is allowed to trade. Nothing here touches the database or
a broker: the inputs are instruments, indicator readings, and outside views.
"""

from __future__ import annotations

from app.domain.fundamentals import FundamentalView
from app.domain.portfolio_selection import (
    MIN_OBSERVATIONS,
    Candidate,
    scan_market,
    select_finalists,
    technical_score,
)
from app.domain.trading import IndicatorEvidence
from app.domain.universe import Instrument


def instrument(symbol: str, category: str = "argentina_stocks") -> Instrument:
    return Instrument(
        symbol=symbol, name=f"{symbol} S.A.", category=category, market="BCBA"
    )


def evidence(
    *,
    last: float | None = 100.0,
    fast: float | None = 110.0,
    slow: float | None = 100.0,
    rsi: float | None = 55.0,
    histogram: float | None = 0.5,
    ret: float | None = 0.08,
    volume: float = 50_000.0,
    observations: int = 60,
) -> IndicatorEvidence:
    from app.domain.money import money

    return IndicatorEvidence(
        last_price=money(str(last)) if last is not None else None,
        ema_fast=fast,
        ema_slow=slow,
        rsi=rsi,
        macd=1.0,
        macd_signal=0.5,
        macd_histogram=histogram,
        atr=1.0,
        average_volume=volume,
        return_fraction=ret,
        observations=observations,
    )


def bullish(**kwargs) -> IndicatorEvidence:
    return evidence(**kwargs)


def bearish(**kwargs) -> IndicatorEvidence:
    defaults = {"fast": 90.0, "slow": 100.0, "histogram": -0.5, "ret": -0.05}
    defaults.update(kwargs)
    return evidence(**defaults)


# --- Stage one: the market scanner ---------------------------------------


def test_the_scanner_keeps_the_instruments_the_market_actually_trades() -> None:
    universe = [instrument(f"IN{index:02d}") for index in range(34)]
    readings = {
        row.symbol: bearish(observations=5 if index == 3 else 60)
        for index, row in enumerate(universe)
    }
    readings["IN05"] = bullish()

    kept = scan_market(universe, readings, limit=12)

    assert len(kept) == 12
    assert kept[0].symbol == "IN05"
    assert "IN03" not in [row.symbol for row in kept]


def test_an_instrument_without_a_usable_price_is_never_a_candidate() -> None:
    readings = {
        "NOPRICE": evidence(last=None),
        "GOOD": bullish(),
    }

    kept = scan_market([instrument("NOPRICE"), instrument("GOOD")], readings, limit=5)

    assert [row.symbol for row in kept] == ["GOOD"]


def test_the_scanner_favours_an_uptrend_and_a_move_already_behind_it() -> None:
    universe = [instrument("FLAT"), instrument("RISING"), instrument("FALLING")]
    readings = {
        "FLAT": evidence(fast=100.0, slow=100.0, ret=0.05),
        "RISING": bullish(ret=0.20),
        "FALLING": bearish(ret=-0.20),
    }

    kept = scan_market(universe, readings, limit=3)

    assert [row.symbol for row in kept] == ["RISING", "FLAT", "FALLING"]


def test_liquidity_breaks_a_tie_between_two_equal_trends() -> None:
    universe = [instrument("THIN"), instrument("DEEP")]
    readings = {
        "THIN": bullish(volume=1_000),
        "DEEP": bullish(volume=900_000),
    }

    kept = scan_market(universe, readings, limit=2)

    assert [row.symbol for row in kept] == ["DEEP", "THIN"]


def test_a_bond_is_never_dropped_for_being_a_bond() -> None:
    # A bond trades less than a share; the scanner compares instruments with
    # each other instead of against a floor no bond could clear.
    universe = [instrument("GGAL", "argentina_stocks"), instrument("AL30", "bonds")]
    readings = {
        "GGAL": bullish(volume=5_000_000),
        "AL30": bullish(volume=10_000),
    }

    kept = scan_market(universe, readings, limit=2)

    assert {row.symbol for row in kept} == {"GGAL", "AL30"}
    assert kept[1].category == "bonds"


def test_the_scanner_honours_its_limit() -> None:
    universe = [instrument(f"IN{index:02d}") for index in range(34)]
    readings = {row.symbol: bullish() for row in universe}

    assert len(scan_market(universe, readings, limit=12)) == 12
    assert len(scan_market(universe, readings, limit=10)) == 10


# --- Stage two: the technical score --------------------------------------


def test_the_technical_score_rewards_an_uptrend_and_penalises_a_downtrend() -> None:
    strong = technical_score(bullish())
    weak = technical_score(bearish())

    assert strong is not None and weak is not None
    assert 0.0 <= weak < strong <= 1.0


def test_the_technical_score_prefers_a_healthy_rsi_to_a_stretched_one() -> None:
    healthy = technical_score(bullish(rsi=55.0))
    stretched = technical_score(bullish(rsi=85.0))

    assert healthy is not None and stretched is not None
    assert healthy > stretched


def test_an_instrument_with_too_little_history_is_not_judged() -> None:
    assert technical_score(bullish(observations=MIN_OBSERVATIONS - 1)) is None
    assert technical_score(None) is None


# --- Stage three: the finalists ------------------------------------------


def finalist_view(score: float) -> FundamentalView:
    return FundamentalView(score=score, summary=f"score {score:.1f}", confidence=0.9)


def test_the_finalists_combine_the_two_views() -> None:
    # SOLID has the better chart, BELOVED has the better outside view. A narrow
    # technical gap must not make the second opinion meaningless.
    universe = [instrument("SOLID"), instrument("BELOVED")]
    readings = {
        "SOLID": evidence(fast=110.0, slow=100.0, histogram=0.0, rsi=55.0, ret=0.0),
        "BELOVED": evidence(fast=90.0, slow=100.0, histogram=-0.5, rsi=55.0, ret=-0.20),
    }
    candidates = scan_market(universe, readings, limit=2)
    views = {
        "SOLID": finalist_view(0.0),
        "BELOVED": finalist_view(1.0),
    }

    finalists = select_finalists(candidates, views, limit=2)

    assert [row.symbol for row in finalists] == ["BELOVED", "SOLID"]
    assert finalists[0].fundamental_score == 1.0
    assert finalists[1].fundamental_summary == "score 0.0"


def test_a_missing_view_does_not_delete_a_candidate() -> None:
    candidates = scan_market([instrument("GGAL")], {"GGAL": bullish()}, limit=5)

    finalists = select_finalists(candidates, {}, limit=5)

    assert [row.symbol for row in finalists] == ["GGAL"]
    assert finalists[0].fundamental_score == 0.5
    assert "No outside view" in finalists[0].fundamental_summary


def test_finalists_are_cut_to_the_limit_with_the_best_first() -> None:
    universe = [instrument(f"IN{index:02d}") for index in range(12)]
    readings = {
        row.symbol: (bullish(ret=0.10) if index % 2 == 0 else bearish(ret=-0.10))
        for index, row in enumerate(universe)
    }
    candidates = scan_market(universe, readings, limit=12)

    finalists = select_finalists(candidates, {}, limit=5)

    assert len(finalists) == 5
    scores = [row.combined_score for row in finalists]
    assert scores == sorted(scores, reverse=True)


def test_a_candidate_its_own_reading_cannot_judge_is_dropped_here() -> None:
    candidates = [
        Candidate(instrument=instrument("GGAL"), evidence=bullish(), scan_score=1.0),
        Candidate(
            instrument=instrument("THIN"), evidence=bearish(observations=5), scan_score=0.1
        ),
    ]

    finalists = select_finalists(candidates, {}, limit=5)

    assert [row.symbol for row in finalists] == ["GGAL"]


def test_a_finalist_carries_its_paper_name_and_category() -> None:
    candidates = scan_market([instrument("AL30", "bonds")], {"AL30": bullish()}, limit=5)

    row = select_finalists(candidates, {}, limit=5)[0]

    assert row.name == "AL30 S.A."
    assert row.category == "bonds"
    assert row.symbol == "AL30"
