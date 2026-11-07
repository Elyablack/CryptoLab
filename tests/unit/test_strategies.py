from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.features.models import FeatureRow
from cryptolab.strategies.breakout import BreakoutStrategy
from cryptolab.strategies.mean_reversion import MeanReversionStrategy
from cryptolab.strategies.models import Signal
from cryptolab.strategies.trend_following import TrendFollowingStrategy


HOUR_MILLISECONDS = 3_600_000


def make_row(index: int) -> FeatureRow:
    open_time = index * HOUR_MILLISECONDS
    return FeatureRow(
        open_time=open_time,
        close_time=open_time + HOUR_MILLISECONDS - 1,
        available_time=open_time + HOUR_MILLISECONDS,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("100"),
        simple_return=Decimal("0"),
        log_return=Decimal("0"),
        sma20=Decimal("100"),
        sma50=Decimal("100"),
        ema20=Decimal("100"),
        rsi14=Decimal("50"),
        atr14=Decimal("2"),
        rvol20=Decimal("1"),
    )


def test_trend_following_buy_requires_confirmed_bullish_cross() -> None:
    history = [make_row(index) for index in range(51)]
    history[-2] = replace(
        history[-2],
        sma20=Decimal("99"),
        sma50=Decimal("100"),
    )
    history[-1] = replace(
        history[-1],
        close=Decimal("103"),
        high=Decimal("104"),
        sma20=Decimal("102"),
        sma50=Decimal("101"),
        rsi14=Decimal("60"),
        rvol20=Decimal("1.2"),
    )

    decision = TrendFollowingStrategy().evaluate("BTCUSDT", "1h", history)

    assert decision.signal is Signal.BUY
    assert decision.diagnostics["sma20"] == Decimal("102")


def test_trend_following_sell_means_reduce_spot_exposure() -> None:
    history = [make_row(index) for index in range(51)]
    history[-2] = replace(
        history[-2],
        sma20=Decimal("101"),
        sma50=Decimal("100"),
    )
    history[-1] = replace(
        history[-1],
        close=Decimal("97"),
        low=Decimal("96"),
        sma20=Decimal("98"),
        sma50=Decimal("99"),
        rsi14=Decimal("40"),
        rvol20=Decimal("1.2"),
    )

    decision = TrendFollowingStrategy().evaluate("BTCUSDT", "1h", history)

    assert decision.signal is Signal.SELL
    assert "reducing or exiting spot exposure" in decision.explanation


def test_trend_following_holds_without_cross_or_confirmation() -> None:
    history = [make_row(index) for index in range(51)]

    assert (
        TrendFollowingStrategy()
        .evaluate("BTCUSDT", "1h", history)
        .signal
        is Signal.HOLD
    )


def test_mean_reversion_requires_recovery_not_only_oversold_state() -> None:
    history = [make_row(index) for index in range(21)]
    history[-2] = replace(
        history[-2],
        close=Decimal("80"),
        low=Decimal("79"),
        sma20=Decimal("100"),
        rsi14=Decimal("25"),
    )
    history[-1] = replace(
        history[-1],
        close=Decimal("81"),
        low=Decimal("80"),
        sma20=Decimal("100"),
        rsi14=Decimal("29"),
        simple_return=Decimal("0.0125"),
    )

    decision = MeanReversionStrategy().evaluate("BTCUSDT", "1h", history)

    assert decision.signal is Signal.HOLD

    history[-1] = replace(history[-1], close=Decimal("85"), rsi14=Decimal("36"))
    decision = MeanReversionStrategy().evaluate("BTCUSDT", "1h", history)
    assert decision.signal is Signal.BUY


def test_mean_reversion_sell_is_confirmed_overbought_reversal() -> None:
    history = [make_row(index) for index in range(21)]
    history[-2] = replace(
        history[-2],
        close=Decimal("120"),
        high=Decimal("121"),
        sma20=Decimal("100"),
        rsi14=Decimal("75"),
    )
    history[-1] = replace(
        history[-1],
        close=Decimal("115"),
        high=Decimal("116"),
        sma20=Decimal("100"),
        rsi14=Decimal("64"),
        simple_return=Decimal("-0.04"),
    )

    decision = MeanReversionStrategy().evaluate("BTCUSDT", "1h", history)

    assert decision.signal is Signal.SELL
    assert "not open a short" in decision.explanation


def test_breakout_threshold_excludes_current_candle() -> None:
    history = [make_row(index) for index in range(4)]
    history[0] = replace(history[0], high=Decimal("101"), low=Decimal("99"))
    history[1] = replace(history[1], high=Decimal("102"), low=Decimal("98"))
    history[2] = replace(history[2], high=Decimal("103"), low=Decimal("97"))
    history[3] = replace(
        history[3],
        close=Decimal("104"),
        high=Decimal("1000"),
        low=Decimal("96"),
        rvol20=Decimal("2"),
        atr14=Decimal("2"),
    )
    strategy = BreakoutStrategy(lookback_period=3)

    decision = strategy.evaluate("BTCUSDT", "1h", history)

    assert decision.signal is Signal.BUY
    assert decision.diagnostics["upper_breakout_level"] == Decimal("103")


def test_breakout_generates_spot_sell_below_prior_range() -> None:
    history = [make_row(index) for index in range(4)]
    history[0] = replace(history[0], high=Decimal("101"), low=Decimal("99"))
    history[1] = replace(history[1], high=Decimal("102"), low=Decimal("98"))
    history[2] = replace(history[2], high=Decimal("103"), low=Decimal("97"))
    history[3] = replace(
        history[3],
        close=Decimal("96"),
        high=Decimal("104"),
        low=Decimal("95"),
        rvol20=Decimal("2"),
        atr14=Decimal("2"),
    )

    decision = BreakoutStrategy(lookback_period=3).evaluate(
        "BTCUSDT",
        "1h",
        history,
    )

    assert decision.signal is Signal.SELL
    assert "not open a short" in decision.explanation


def test_breakout_does_not_repeat_continuing_buy_state() -> None:
    history = [make_row(index) for index in range(5)]
    history[0] = replace(history[0], high=Decimal("101"))
    history[1] = replace(history[1], high=Decimal("102"))
    history[2] = replace(history[2], high=Decimal("103"))
    history[3] = replace(history[3], close=Decimal("104"), high=Decimal("105"))
    history[4] = replace(
        history[4],
        close=Decimal("106"),
        high=Decimal("107"),
        rvol20=Decimal("2"),
        atr14=Decimal("2"),
    )

    decision = BreakoutStrategy(lookback_period=3).evaluate(
        "BTCUSDT",
        "1h",
        history,
    )

    assert decision.signal is Signal.HOLD


@pytest.mark.parametrize(
    ("relative_volume", "atr"),
    [
        (Decimal("1.49"), Decimal("2")),
        (Decimal("2"), Decimal("0.1")),
    ],
)
def test_breakout_requires_volume_and_atr_confirmation(
    relative_volume: Decimal,
    atr: Decimal,
) -> None:
    history = [make_row(index) for index in range(4)]
    history[0] = replace(history[0], high=Decimal("101"))
    history[1] = replace(history[1], high=Decimal("102"))
    history[2] = replace(history[2], high=Decimal("103"))
    history[3] = replace(
        history[3],
        close=Decimal("104"),
        high=Decimal("105"),
        rvol20=relative_volume,
        atr14=atr,
    )

    decision = BreakoutStrategy(lookback_period=3).evaluate(
        "BTCUSDT",
        "1h",
        history,
    )

    assert decision.signal is Signal.HOLD


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TrendFollowingStrategy(
            buy_rsi_min=Decimal("80"),
            buy_rsi_max=Decimal("70"),
        ),
        lambda: MeanReversionStrategy(minimum_sma_deviation=Decimal("0")),
        lambda: BreakoutStrategy(lookback_period=0),
        lambda: BreakoutStrategy(minimum_atr_percent=Decimal("-0.01")),
    ],
)
def test_strategies_reject_invalid_parameters(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]

