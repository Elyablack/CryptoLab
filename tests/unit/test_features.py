from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.analytics.analyzer import MarketAnalyzer
from cryptolab.features.builder import FeatureBuilder
from cryptolab.market_data.models import Candle


HOUR_MILLISECONDS = 3_600_000


def make_candles(
    count: int,
    *,
    start_time: int = 0,
) -> list[Candle]:
    candles = []
    for index in range(count):
        close = Decimal(100 + index)
        volume = Decimal(100 + index)
        open_time = start_time + index * HOUR_MILLISECONDS
        candles.append(
            Candle(
                open_time=open_time,
                open=close - Decimal("1"),
                high=close + Decimal("2"),
                low=close - Decimal("2"),
                close=close,
                volume=volume,
                close_time=open_time + HOUR_MILLISECONDS - 1,
                quote_volume=volume * close,
                number_of_trades=100 + index,
                taker_buy_base_volume=volume / 2,
                taker_buy_quote_volume=volume * close / 2,
            )
        )
    return candles


def test_feature_builder_preserves_alignment_and_warm_up_values() -> None:
    candles = make_candles(60)
    as_of_time = candles[-1].close_time + 1

    rows = FeatureBuilder().build(
        candles,
        interval="1h",
        as_of_time=as_of_time,
    )

    assert len(rows) == len(candles)
    assert rows[0].open == candles[0].open
    assert rows[0].high == candles[0].high
    assert rows[0].low == candles[0].low
    assert rows[0].close == candles[0].close
    assert rows[0].available_time == rows[0].close_time + 1
    assert rows[0].simple_return is None
    assert rows[1].simple_return == Decimal("0.01")
    assert rows[18].sma20 is None
    assert rows[19].sma20 is not None
    assert rows[18].ema20 is None
    assert rows[19].ema20 is not None
    assert rows[13].atr14 is not None
    assert rows[13].rsi14 is None
    assert rows[14].rsi14 is not None
    assert rows[19].rvol20 is None
    assert rows[20].rvol20 is not None
    assert rows[48].sma50 is None
    assert rows[49].sma50 is not None


def test_last_feature_values_match_market_analysis_report() -> None:
    candles = make_candles(60)
    as_of_time = candles[-1].close_time + 1

    row = FeatureBuilder().build(
        candles,
        interval="1h",
        as_of_time=as_of_time,
    )[-1]
    report = MarketAnalyzer().analyze(
        "BTCUSDT",
        "1h",
        candles,
        as_of_time=as_of_time,
    )

    assert row.sma20 == report.sma20
    assert row.sma50 == report.sma50
    assert row.ema20 == report.ema20
    assert row.rsi14 == report.rsi14
    assert row.atr14 == report.atr14
    assert row.rvol20 == report.relative_volume20


def test_modifying_future_candle_does_not_change_previous_features() -> None:
    candles = make_candles(60)
    as_of_time = candles[-1].close_time + 1
    baseline = FeatureBuilder().build(
        candles,
        interval="1h",
        as_of_time=as_of_time,
    )

    changed = list(candles)
    changed_close = changed[40].close + Decimal("25")
    changed[40] = replace(
        changed[40],
        close=changed_close,
        high=changed_close + Decimal("2"),
    )
    modified = FeatureBuilder().build(
        changed,
        interval="1h",
        as_of_time=as_of_time,
    )

    assert modified[:40] == baseline[:40]
    assert modified[40:] != baseline[40:]


def test_feature_builder_supports_explicit_simulation_time() -> None:
    future_start = 4_102_444_800_000  # 2100-01-01T00:00:00Z
    candles = make_candles(3, start_time=future_start)
    simulation_time = candles[-1].close_time + 1

    rows = FeatureBuilder().build(
        candles,
        interval="1h",
        as_of_time=simulation_time,
    )

    assert len(rows) == 3
    assert rows[-1].available_time == simulation_time


def test_feature_builder_rejects_candle_unavailable_at_simulation_time() -> None:
    candles = make_candles(3)

    with pytest.raises(ValueError, match="not closed"):
        FeatureBuilder().build(
            candles,
            interval="1h",
            as_of_time=candles[-1].close_time,
        )


def test_feature_builder_rejects_gap_and_invalid_ohlcv() -> None:
    candles = make_candles(3)
    candles[2] = replace(
        candles[2],
        open_time=candles[2].open_time + HOUR_MILLISECONDS,
        close_time=candles[2].close_time + HOUR_MILLISECONDS,
    )

    with pytest.raises(ValueError, match="consecutive"):
        FeatureBuilder().build(candles, interval="1h", as_of_time=20_000_000)

    invalid = make_candles(3)
    invalid[1] = replace(invalid[1], low=Decimal("1000"))
    with pytest.raises(ValueError, match="price cannot be"):
        FeatureBuilder().build(invalid, interval="1h", as_of_time=20_000_000)


def test_feature_builder_infers_interval_for_existing_callers() -> None:
    candles = make_candles(3)

    rows = FeatureBuilder().build(
        candles,
        as_of_time=candles[-1].close_time + 1,
    )

    assert len(rows) == 3


def test_feature_builder_handles_empty_input() -> None:
    assert FeatureBuilder().build([]) == []

