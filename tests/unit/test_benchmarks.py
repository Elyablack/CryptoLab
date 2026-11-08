from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.backtesting import BacktestConfig, calculate_benchmarks
from cryptolab.market_data.models import Candle


HOUR_MILLISECONDS = 3_600_000


def make_candles(prices: list[str]) -> list[Candle]:
    candles = []
    for index, price_text in enumerate(prices):
        price = Decimal(price_text)
        open_time = index * HOUR_MILLISECONDS
        candles.append(Candle(
            open_time=open_time,
            open=price,
            high=price + Decimal("1"),
            low=price - Decimal("1"),
            close=price,
            volume=Decimal("10"),
            close_time=open_time + HOUR_MILLISECONDS - 1,
            quote_volume=price * Decimal("10"),
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ))
    return candles


def test_buy_and_hold_uses_comparable_entry_cost_and_last_close_mark() -> None:
    candles = make_candles(["100", "110", "120"])
    config = BacktestConfig(
        initial_cash=Decimal("1000"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.0005"),
    )

    buy_and_hold, cash_only = calculate_benchmarks(
        candles,
        "BTCUSDT",
        "1h",
        config=config,
    )

    notional = Decimal("1000") / Decimal("1.001")
    fee = Decimal("1000") - notional
    quantity = notional / Decimal("100.05")
    final_equity = quantity * Decimal("120")

    assert buy_and_hold.final_equity == final_equity
    assert buy_and_hold.total_pnl == final_equity - Decimal("1000")
    assert buy_and_hold.realized_pnl == 0
    assert buy_and_hold.unrealized_pnl == buy_and_hold.total_pnl
    assert buy_and_hold.fees_paid == fee
    assert buy_and_hold.exposure_percentage == 1
    assert buy_and_hold.completed_trade_count == 0
    assert buy_and_hold.win_rate is None
    assert buy_and_hold.has_open_position

    assert cash_only.final_equity == Decimal("1000")
    assert cash_only.total_pnl == 0
    assert cash_only.fees_paid == 0
    assert cash_only.exposure_percentage == 0
    assert not cash_only.has_open_position


def test_buy_and_hold_has_no_hypothetical_exit_cost() -> None:
    candles = make_candles(["100", "100"])
    config = BacktestConfig(
        fee_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
    )

    buy_and_hold, _ = calculate_benchmarks(
        candles,
        "BTCUSDT",
        "1h",
        config=config,
    )

    assert buy_and_hold.final_equity == Decimal("1000")
    assert buy_and_hold.total_pnl == 0
    assert buy_and_hold.fees_paid == 0


def test_benchmarks_are_deterministic_and_period_scoped() -> None:
    candles = make_candles(["100", "90", "200"])
    config = BacktestConfig(
        fee_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
    )

    first = calculate_benchmarks(
        candles[:2],
        "BTCUSDT",
        "1h",
        config=config,
    )
    candles[2] = replace(
        candles[2],
        open=Decimal("1000"),
        high=Decimal("1001"),
        low=Decimal("999"),
        close=Decimal("1000"),
    )
    second = calculate_benchmarks(
        candles[:2],
        "BTCUSDT",
        "1h",
        config=config,
    )

    assert first == second
    assert first[0].start_time == 0
    assert first[0].end_time == 2 * HOUR_MILLISECONDS
    assert first[0].final_equity == Decimal("900")
    assert first[0].maximum_equity_drawdown == Decimal("-0.1")


def test_benchmarks_reject_nonconsecutive_input() -> None:
    candles = make_candles(["100", "101", "102"])

    with pytest.raises(ValueError, match="consecutive"):
        calculate_benchmarks(
            [candles[0], candles[2]],
            "BTCUSDT",
            "1h",
        )
