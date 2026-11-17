from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    ProtectiveStopFill,
)
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.models import Candle
from cryptolab.strategies import (
    Signal,
    StrategyRunner,
    VolatilityCompressionBreakoutStrategy,
)


HOUR = 3_600_000


def make_row(index: int) -> FeatureRow:
    open_time = index * HOUR
    return FeatureRow(
        open_time=open_time,
        close_time=open_time + HOUR - 1,
        available_time=open_time + HOUR,
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


def make_buy_setup() -> list[FeatureRow]:
    rows = [make_row(index) for index in range(158)]
    for index in range(133, 157):
        rows[index] = replace(rows[index], atr14=Decimal("1"))
    rows[-1] = replace(
        rows[-1],
        open=Decimal("100.5"),
        high=Decimal("103"),
        low=Decimal("100"),
        close=Decimal("102"),
        atr14=Decimal("1.5"),
        rvol20=Decimal("1.3"),
        simple_return=Decimal("0.02"),
    )
    return rows


def make_candles(count: int) -> list[Candle]:
    result = []
    for index in range(count):
        open_time = index * HOUR
        result.append(Candle(
            open_time=open_time,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("100"),
            close_time=open_time + HOUR - 1,
            quote_volume=Decimal("10000"),
            number_of_trades=10,
            taker_buy_base_volume=Decimal("50"),
            taker_buy_quote_volume=Decimal("5000"),
        ))
    return result


def test_minimum_history_matches_atr_warmup_and_complete_windows() -> None:
    strategy = VolatilityCompressionBreakoutStrategy()
    candles = make_candles(158)
    rows = FeatureBuilder().build(
        candles,
        interval="1h",
        as_of_time=candles[-1].close_time + 1,
    )

    assert strategy.minimum_history == 158
    assert rows[12].atr14 is None
    assert rows[13].atr14 is not None
    decision = strategy.evaluate("BTCUSDT", "1h", rows)
    assert decision.diagnostics["baseline_observations"] == 120
    assert decision.diagnostics["compression_observations"] == 24

    with pytest.raises(ValueError, match="requires 158"):
        strategy.evaluate("BTCUSDT", "1h", rows[:-1])


def test_confirmed_compression_breakout_emits_buy() -> None:
    rows = make_buy_setup()

    decision = VolatilityCompressionBreakoutStrategy().evaluate(
        "BTCUSDT",
        "1h",
        rows,
    )

    assert decision.signal is Signal.BUY
    assert decision.signal_available_time == rows[-1].available_time
    assert decision.diagnostics["compression_confirmed"] is True
    assert decision.diagnostics["atr_expansion_confirmed"] is True
    assert decision.diagnostics["volume_confirmed"] is True
    assert decision.diagnostics["buy_blocker"] == "none"


def test_compression_and_expansion_are_diagnosed_independently() -> None:
    no_compression = make_buy_setup()
    for index in range(133, 157):
        no_compression[index] = replace(
            no_compression[index],
            atr14=Decimal("1.8"),
        )
    no_compression[-1] = replace(
        no_compression[-1],
        atr14=Decimal("3"),
    )

    compression_decision = VolatilityCompressionBreakoutStrategy().evaluate(
        "BTCUSDT", "1h", no_compression
    )

    assert compression_decision.signal is Signal.HOLD
    assert compression_decision.diagnostics["compression_confirmed"] is False
    assert compression_decision.diagnostics["atr_expansion_confirmed"] is True
    assert compression_decision.diagnostics["buy_blocker"] == "compression"

    no_expansion = make_buy_setup()
    no_expansion[-1] = replace(no_expansion[-1], atr14=Decimal("1.1"))

    expansion_decision = VolatilityCompressionBreakoutStrategy().evaluate(
        "BTCUSDT", "1h", no_expansion
    )

    assert expansion_decision.signal is Signal.HOLD
    assert expansion_decision.diagnostics["compression_confirmed"] is True
    assert expansion_decision.diagnostics["atr_expansion_confirmed"] is False
    assert expansion_decision.diagnostics["buy_blocker"] == "atr_expansion"


def test_relative_volume_can_be_the_final_buy_blocker() -> None:
    rows = make_buy_setup()
    rows[-1] = replace(rows[-1], rvol20=Decimal("1.24"))

    decision = VolatilityCompressionBreakoutStrategy().evaluate(
        "BTCUSDT", "1h", rows
    )

    assert decision.signal is Signal.HOLD
    assert decision.diagnostics["buy_blocker"] == "relative_volume"


def test_new_lower_range_breakdown_emits_sell() -> None:
    rows = make_buy_setup()
    rows[-1] = replace(
        rows[-1],
        open=Decimal("99"),
        high=Decimal("100"),
        low=Decimal("97"),
        close=Decimal("98"),
    )

    decision = VolatilityCompressionBreakoutStrategy().evaluate(
        "BTCUSDT", "1h", rows
    )

    assert decision.signal is Signal.SELL
    assert decision.diagnostics["new_lower_breakdown"] is True
    assert "not open a short" in decision.explanation


def test_continued_upper_breakout_does_not_repeat_buy() -> None:
    rows = make_buy_setup()
    rows[-2] = replace(
        rows[-2],
        open=Decimal("100"),
        high=Decimal("102.5"),
        low=Decimal("99.5"),
        close=Decimal("102"),
    )
    rows[-1] = replace(
        rows[-1],
        open=Decimal("102"),
        high=Decimal("104"),
        low=Decimal("101.5"),
        close=Decimal("103"),
    )

    decision = VolatilityCompressionBreakoutStrategy().evaluate(
        "BTCUSDT", "1h", rows
    )

    assert decision.signal is Signal.HOLD
    assert decision.diagnostics["upper_breakout_confirmed"] is True
    assert decision.diagnostics["new_upper_breakout"] is False
    assert decision.diagnostics["buy_blocker"] == "new_breakout_event"


def test_future_row_cannot_change_prior_decision() -> None:
    rows = make_buy_setup()
    future = replace(
        rows[-1],
        open_time=158 * HOUR,
        close_time=159 * HOUR - 1,
        available_time=159 * HOUR,
        open=Decimal("1000"),
        high=Decimal("1100"),
        low=Decimal("900"),
        close=Decimal("1050"),
    )
    runner = StrategyRunner([VolatilityCompressionBreakoutStrategy()])

    baseline = runner.run("BTCUSDT", "1h", rows, as_of_time=158 * HOUR)
    with_future = runner.run(
        "BTCUSDT",
        "1h",
        [*rows, future],
        as_of_time=158 * HOUR,
    )

    assert with_future == baseline
    assert baseline[-1].signal is Signal.BUY


def test_buy_executes_next_open_and_protective_stop_is_engine_owned() -> None:
    rows = make_buy_setup()
    execution_row = replace(
        rows[-1],
        open_time=158 * HOUR,
        close_time=159 * HOUR - 1,
        available_time=159 * HOUR,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("96"),
        close=Decimal("98"),
    )

    report = BacktestEngine(BacktestConfig(
        fee_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
        stop_loss_rate=Decimal("0.03"),
    )).run(
        VolatilityCompressionBreakoutStrategy(),
        "BTCUSDT",
        "1h",
        [*rows, execution_row],
    )

    assert report.fills[0].execution_open_time == execution_row.open_time
    assert report.fills[0].execution_price == Decimal("100")
    assert isinstance(report.fills[1], ProtectiveStopFill)
    assert report.fills[1].execution_price == Decimal("97.00")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"baseline_window": 0},
        {"compression_window": 0},
        {"exit_lookback": 0},
        {"maximum_compression_ratio": Decimal("0")},
        {"minimum_atr_expansion_ratio": Decimal("NaN")},
        {"minimum_relative_volume": Decimal("-1")},
    ],
)
def test_invalid_parameters_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        VolatilityCompressionBreakoutStrategy(**kwargs)
