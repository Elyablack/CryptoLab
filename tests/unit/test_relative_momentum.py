from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    PortfolioBacktestInput,
)
from cryptolab.features.models import FeatureRow
from cryptolab.strategies import (
    RelativeMomentumRotationPlanner,
    RotationTarget,
    Signal,
)


HOUR = 3_600_000


def make_rows(prices: list[str]) -> tuple[FeatureRow, ...]:
    rows = []
    for index, raw_price in enumerate(prices):
        price = Decimal(raw_price)
        open_time = index * HOUR
        rows.append(FeatureRow(
            open_time=open_time,
            close_time=open_time + HOUR - 1,
            available_time=open_time + HOUR,
            open=price,
            high=price + Decimal("1"),
            low=price - Decimal("1"),
            close=price,
            volume=Decimal("10"),
            simple_return=None,
            log_return=None,
            sma20=None,
            sma50=None,
            ema20=None,
            rsi14=None,
            atr14=None,
            rvol20=None,
        ))
    return tuple(rows)


def transition_rows() -> tuple[tuple[FeatureRow, ...], tuple[FeatureRow, ...]]:
    return (
        make_rows(["100", "100", "120", "120", "110", "110", "90", "90"]),
        make_rows(["100", "100", "110", "110", "130", "130", "100", "100"]),
    )


def test_planner_builds_weekly_relative_momentum_transitions() -> None:
    btc, eth = transition_rows()
    plan = RelativeMomentumRotationPlanner(
        lookback_candles=2,
        rebalance_candles=2,
    ).build(btc, eth)

    assert [review.target for review in plan.reviews] == [
        RotationTarget.BTC,
        RotationTarget.ETH,
        RotationTarget.CASH,
    ]
    assert plan.reviews[0].btc_momentum == Decimal("0.2")
    assert plan.reviews[0].eth_momentum == Decimal("0.1")
    assert plan.target_change_count == 3
    assert plan.asset_rotation_count == 1
    assert [
        (signal.symbol, signal.signal)
        for signal in plan.leg_signals
    ] == [
        ("BTCUSDT", Signal.BUY),
        ("BTCUSDT", Signal.SELL),
        ("ETHUSDT", Signal.BUY),
        ("ETHUSDT", Signal.SELL),
    ]


def test_unchanged_target_does_not_emit_duplicate_orders() -> None:
    btc = make_rows(["100", "100", "120", "120", "144", "144", "170", "170"])
    eth = make_rows(["100", "100", "110", "110", "115", "115", "120", "120"])
    plan = RelativeMomentumRotationPlanner(
        lookback_candles=2,
        rebalance_candles=2,
    ).build(btc, eth)

    assert [review.target for review in plan.reviews] == [
        RotationTarget.BTC,
        RotationTarget.BTC,
        RotationTarget.BTC,
    ]
    assert len(plan.leg_signals) == 1
    assert plan.leg_signals[0].signal is Signal.BUY


def test_planner_uses_fixed_hourly_schedule_and_30_day_lookback() -> None:
    rows = make_rows(["100"] * 900)

    plan = RelativeMomentumRotationPlanner().build(rows, rows)

    assert [review.candle_open_time // HOUR for review in plan.reviews] == [
        720,
        888,
    ]
    assert plan.leg_signals == ()


def test_future_price_changes_do_not_change_previous_review() -> None:
    btc, eth = transition_rows()
    planner = RelativeMomentumRotationPlanner(
        lookback_candles=2,
        rebalance_candles=2,
    )
    baseline = planner.build(btc, eth)
    changed_eth = tuple(
        replace(row, open=Decimal("500"), high=Decimal("501"),
                low=Decimal("499"), close=Decimal("500"))
        if index > 2 else row
        for index, row in enumerate(eth)
    )

    modified = planner.build(btc, changed_eth)

    assert modified.reviews[0] == baseline.reviews[0]
    assert modified.leg_signals[0] == baseline.leg_signals[0]


def test_isolated_period_starts_in_cash_with_prior_rows_as_lookback() -> None:
    btc, eth = transition_rows()
    planner = RelativeMomentumRotationPlanner(
        lookback_candles=2,
        rebalance_candles=2,
    )

    plan = planner.build(
        btc,
        eth,
        trading_start_time=4 * HOUR,
        trading_end_time=8 * HOUR,
    )

    assert plan.reviews[0].previous_target is RotationTarget.CASH
    assert plan.reviews[0].target is RotationTarget.ETH
    assert [
        (signal.symbol, signal.signal)
        for signal in plan.leg_signals[:1]
    ] == [("ETHUSDT", Signal.BUY)]
    assert plan == planner.build(
        btc,
        eth,
        trading_start_time=4 * HOUR,
        trading_end_time=8 * HOUR,
    )


def test_planner_rejects_missing_or_misaligned_history() -> None:
    btc, eth = transition_rows()
    planner = RelativeMomentumRotationPlanner(
        lookback_candles=2,
        rebalance_candles=2,
    )

    with pytest.raises(ValueError, match="equal length"):
        planner.build(btc, eth[:-1])

    shifted = list(eth)
    shifted[3] = replace(
        shifted[3],
        open_time=shifted[3].open_time + HOUR,
        close_time=shifted[3].close_time + HOUR,
        available_time=shifted[3].available_time + HOUR,
    )
    with pytest.raises(ValueError, match="misaligned"):
        planner.build(btc, tuple(shifted))


def test_rotation_executes_next_open_with_shared_accounting() -> None:
    btc, eth = transition_rows()
    plan = RelativeMomentumRotationPlanner(
        lookback_candles=2,
        rebalance_candles=2,
    ).build(btc, eth)
    engine = BacktestEngine(BacktestConfig(
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.0005"),
    ))
    inputs = (
        PortfolioBacktestInput(
            strategy=plan.strategy_for("BTCUSDT"),
            symbol="BTCUSDT",
            interval="1h",
            rows=btc,
            exchange="bybit",
        ),
        PortfolioBacktestInput(
            strategy=plan.strategy_for("ETHUSDT"),
            symbol="ETHUSDT",
            interval="1h",
            rows=eth,
            exchange="bybit",
        ),
    )

    first = engine.run_portfolio(inputs)
    second = engine.run_portfolio(tuple(reversed(inputs)))

    assert first == second
    assert [fill.instrument.symbol for fill in first.fills] == [
        "BTCUSDT",
        "BTCUSDT",
        "ETHUSDT",
        "ETHUSDT",
    ]
    assert [fill.side for fill in first.fills] == [
        Signal.BUY,
        Signal.SELL,
        Signal.BUY,
        Signal.SELL,
    ]
    assert [fill.execution_open_time for fill in first.fills] == [
        3 * HOUR,
        5 * HOUR,
        5 * HOUR,
        7 * HOUR,
    ]
    assert first.positions == ()
    assert first.final_quote_balance >= 0
    assert len(first.completed_trades) == 2
    assert len({intent.intent_id for intent in first.order_intents}) == 4
    assert sum((fill.fee for fill in first.fills), Decimal("0")) > 0
    assert all(point.quote_balance >= 0 for point in first.equity_curve)
    assert all(len(point.positions) <= 1 for point in first.equity_curve)
