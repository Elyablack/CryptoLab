from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    ProtectiveStopFill,
)
from cryptolab.features.models import FeatureRow
from cryptolab.strategies import (
    Signal,
    StrategyRunner,
    TrendPullbackExitPolicy,
    TrendPullbackStrategy,
)


HOUR = 3_600_000


def make_rows(count: int = 60) -> list[FeatureRow]:
    rows = []
    for index in range(count):
        open_time = index * HOUR
        sma50 = Decimal("100") + Decimal(index) / 100
        close = sma50 + 3
        rows.append(FeatureRow(
            open_time=open_time,
            close_time=open_time + HOUR - 1,
            available_time=open_time + HOUR,
            open=close - Decimal("0.2"),
            high=close + Decimal("0.5"),
            low=close - Decimal("0.5"),
            close=close,
            volume=Decimal("10"),
            simple_return=Decimal("0.002"),
            log_return=Decimal("0.001998"),
            sma20=sma50 + 2,
            sma50=sma50,
            ema20=sma50 + 1,
            rsi14=Decimal("60"),
            atr14=Decimal("1"),
            rvol20=Decimal("1"),
        ))
    return rows


def make_buy_setup() -> list[FeatureRow]:
    rows = make_rows()
    origin = rows[-2]
    rows[-2] = replace(
        origin,
        open=Decimal("101.5"),
        high=Decimal("102"),
        low=Decimal("101"),
        close=Decimal("101.2"),
        rsi14=Decimal("48"),
        simple_return=Decimal("-0.004"),
    )
    current = rows[-1]
    rows[-1] = replace(
        current,
        open=Decimal("101.8"),
        high=Decimal("103"),
        low=Decimal("101.7"),
        close=Decimal("102.5"),
        rsi14=Decimal("55"),
        simple_return=Decimal("0.01"),
        rvol20=Decimal("1"),
        atr14=Decimal("1"),
    )
    return rows


def test_confirmed_uptrend_pullback_emits_buy() -> None:
    rows = make_buy_setup()

    decision = TrendPullbackStrategy().evaluate("BTCUSDT", "1h", rows)

    assert decision.signal is Signal.BUY
    assert decision.candle_open_time == rows[-1].open_time
    assert decision.signal_available_time == rows[-1].available_time
    assert decision.diagnostics["uptrend"] is True
    assert decision.diagnostics["confirmation"] is True
    assert (
        decision.diagnostics["pullback_origin_open_time"]
        == rows[-2].open_time
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: replace(row, rvol20=Decimal("0.79")),
        lambda row: replace(row, rsi14=Decimal("66")),
        lambda row: replace(row, atr14=row.close * Decimal("0.026")),
        lambda row: replace(row, close=row.ema20 - Decimal("0.1")),
    ],
)
def test_entry_filters_reject_unconfirmed_recovery(mutation) -> None:
    rows = make_buy_setup()
    rows[-1] = mutation(rows[-1])

    decision = TrendPullbackStrategy().evaluate("BTCUSDT", "1h", rows)

    assert decision.signal is Signal.HOLD


def test_trend_break_emits_sell_without_implying_short() -> None:
    rows = make_rows()
    current = rows[-1]
    rows[-1] = replace(
        current,
        close=current.sma50 - 1,
        high=current.sma50 + 1,
        low=current.sma50 - 2,
        sma20=rows[-2].sma20 - 1,
    )

    decision = TrendPullbackStrategy().evaluate("BTCUSDT", "1h", rows)

    assert decision.signal is Signal.SELL
    assert "not open a short" in decision.explanation
    assert decision.diagnostics["price_broke_trend"] is True


def test_entry_only_policy_suppresses_strategy_sell() -> None:
    rows = make_rows()
    current = rows[-1]
    rows[-1] = replace(
        current,
        close=current.sma50 - 1,
        high=current.sma50 + 1,
        low=current.sma50 - 2,
        sma20=rows[-2].sma20 - 1,
    )

    decision = TrendPullbackStrategy(
        exit_policy=TrendPullbackExitPolicy.ENTRY_ONLY,
    ).evaluate("BTCUSDT", "1h", rows)

    assert decision.signal is Signal.HOLD
    assert decision.diagnostics["price_broke_trend"] is True


def test_long_term_policy_requires_price_and_sma20_below_sma50() -> None:
    rows = make_rows()
    current = rows[-1]
    rows[-1] = replace(
        current,
        close=current.sma50 - 1,
        high=current.sma50 + 1,
        low=current.sma50 - 2,
        sma20=current.sma50 - Decimal("0.1"),
    )

    decision = TrendPullbackStrategy(
        exit_policy=TrendPullbackExitPolicy.LONG_TERM_TREND,
    ).evaluate("BTCUSDT", "1h", rows)

    assert decision.signal is Signal.SELL
    assert decision.diagnostics["long_term_trend_broken"] is True


def test_used_pullback_does_not_emit_repeated_buy() -> None:
    rows = make_buy_setup()
    origin = rows[-4]
    rows[-4] = replace(
        origin,
        high=Decimal("102"),
        low=Decimal("101"),
        close=Decimal("101.2"),
        rsi14=Decimal("48"),
    )
    rows[-3] = replace(
        rows[-3],
        close=Decimal("103"),
        high=Decimal("103.2"),
        low=Decimal("102"),
        rsi14=Decimal("55"),
    )
    rows[-2] = replace(rows[-2], low=Decimal("102.1"), rsi14=Decimal("60"))

    decision = TrendPullbackStrategy().evaluate("BTCUSDT", "1h", rows)

    assert decision.signal is Signal.HOLD
    assert decision.diagnostics["pullback_origin_open_time"] is None


def test_current_candle_cannot_be_its_own_pullback_origin() -> None:
    rows = make_rows()
    current = rows[-1]
    rows[-1] = replace(
        current,
        high=rows[-2].high + Decimal("1"),
        low=current.ema20 - Decimal("1"),
        close=rows[-2].high + Decimal("0.1"),
        rsi14=Decimal("55"),
        simple_return=Decimal("0.01"),
        rvol20=Decimal("1"),
    )

    decision = TrendPullbackStrategy().evaluate("BTCUSDT", "1h", rows)

    assert decision.signal is Signal.HOLD
    assert decision.diagnostics["pullback_origin_open_time"] is None


def test_future_row_cannot_change_prior_pullback_decision() -> None:
    rows = make_buy_setup()
    future = replace(
        rows[-1],
        open_time=60 * HOUR,
        close_time=61 * HOUR - 1,
        available_time=61 * HOUR,
        open=Decimal("1000"),
        high=Decimal("1100"),
        low=Decimal("900"),
        close=Decimal("1050"),
    )
    runner = StrategyRunner([TrendPullbackStrategy()])

    baseline = runner.run("BTCUSDT", "1h", rows, as_of_time=60 * HOUR)
    with_future = runner.run(
        "BTCUSDT",
        "1h",
        [*rows, future],
        as_of_time=60 * HOUR,
    )

    assert with_future == baseline
    assert baseline[-1].signal is Signal.BUY


def test_warmup_boundary_is_hold_and_direct_evaluation_rejects_it() -> None:
    rows = make_rows(55)
    strategy = TrendPullbackStrategy()

    decisions = StrategyRunner([strategy]).run("BTCUSDT", "1h", rows)

    assert all(item.signal is Signal.HOLD for item in decisions)
    with pytest.raises(ValueError, match="requires 56"):
        strategy.evaluate("BTCUSDT", "1h", rows)


def test_buy_executes_next_open_then_fixed_stop_can_trigger_intrabar() -> None:
    rows = make_buy_setup()
    next_row = replace(
        rows[-1],
        open_time=60 * HOUR,
        close_time=61 * HOUR - 1,
        available_time=61 * HOUR,
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
        TrendPullbackStrategy(),
        "BTCUSDT",
        "1h",
        [*rows, next_row],
    )

    assert report.fills[0].execution_open_time == next_row.open_time
    assert report.fills[0].execution_price == Decimal("100")
    assert isinstance(report.fills[1], ProtectiveStopFill)
    assert report.fills[1].execution_price == Decimal("97.00")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trend_slope_lookback": 0},
        {"pullback_lookback": 0},
        {"pullback_rsi_min": Decimal("60")},
        {"confirmation_rsi_max": Decimal("101")},
        {"minimum_relative_volume": Decimal("-0.1")},
        {"maximum_atr_percent": Decimal("0")},
        {"exit_policy": "baseline"},
    ],
)
def test_invalid_pullback_parameters_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        TrendPullbackStrategy(**kwargs)
