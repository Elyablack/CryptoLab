from collections.abc import Sequence
from decimal import Decimal

import pytest

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    FixedFractionPositionSizer,
    PortfolioBacktestInput,
)
from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal


HOUR = 3_600_000


def make_rows(prices: list[str], *, offset: int = 0) -> tuple[FeatureRow, ...]:
    rows = []
    for index, value in enumerate(prices):
        price = Decimal(value)
        open_time = offset + index * HOUR
        rows.append(FeatureRow(
            open_time=open_time,
            close_time=open_time + HOUR - 1,
            available_time=open_time + HOUR,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=Decimal("10"),
            simple_return=Decimal("0"),
            log_return=Decimal("0"),
            sma20=price,
            sma50=price,
            ema20=price,
            rsi14=Decimal("50"),
            atr14=Decimal("2"),
            rvol20=Decimal("1"),
        ))
    return tuple(rows)


class ScriptedStrategy:
    minimum_history = 1

    def __init__(self, name: str, signals: Sequence[Signal]) -> None:
        self.name = name
        self._signals = tuple(signals)

    def evaluate(self, symbol, interval, history):
        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=history[-1],
            signal=self._signals[len(history) - 1],
            explanation="shared portfolio test",
        )


def input_for(symbol: str, prices: list[str]) -> PortfolioBacktestInput:
    return PortfolioBacktestInput(
        strategy=ScriptedStrategy(
            f"strategy_{symbol.lower()}",
            [Signal.BUY, Signal.SELL, Signal.HOLD],
        ),
        symbol=symbol,
        interval="1h",
        rows=make_rows(prices),
        exchange="bybit",
    )


def test_shared_cash_uses_stable_order_and_marks_all_positions() -> None:
    engine = BacktestEngine(
        BacktestConfig(fee_rate=Decimal("0"), slippage_rate=Decimal("0")),
        position_sizer=FixedFractionPositionSizer(Decimal("0.5")),
    )
    btc = input_for("BTCUSDT", ["90", "100", "110"])
    eth = input_for("ETHUSDT", ["45", "50", "55"])

    report = engine.run_portfolio([eth, btc])

    assert [fill.instrument.symbol for fill in report.fills] == [
        "BTCUSDT",
        "ETHUSDT",
        "BTCUSDT",
        "ETHUSDT",
    ]
    assert report.fills[0].quote_notional == Decimal("500.0")
    assert report.fills[1].quote_notional == Decimal("250.00")
    assert report.final_quote_balance == Decimal("1075.00")
    assert report.positions == ()
    assert report.final_equity == Decimal("1075.00")
    assert report.realized_pnl == Decimal("75.00")
    assert report.unrealized_pnl == 0
    assert report.total_pnl == Decimal("75.00")
    assert len(report.completed_trades) == 2
    assert len(report.position_cycles) == 2
    assert all(not cycle.is_open for cycle in report.position_cycles)
    assert all(point.quote_balance >= 0 for point in report.equity_curve)


def test_input_order_does_not_change_simultaneous_allocation() -> None:
    engine = BacktestEngine(
        BacktestConfig(fee_rate=Decimal("0"), slippage_rate=Decimal("0")),
        position_sizer=FixedFractionPositionSizer(Decimal("0.5")),
    )
    btc = input_for("BTCUSDT", ["90", "100", "110"])
    eth = input_for("ETHUSDT", ["45", "50", "55"])

    first = engine.run_portfolio([btc, eth])
    second = engine.run_portfolio([eth, btc])

    assert first == second


def test_open_multi_symbol_positions_reconcile_equity() -> None:
    engine = BacktestEngine(
        BacktestConfig(fee_rate=Decimal("0"), slippage_rate=Decimal("0")),
        position_sizer=FixedFractionPositionSizer(Decimal("0.5")),
    )
    signals = [Signal.BUY, Signal.HOLD, Signal.HOLD]
    btc = PortfolioBacktestInput(
        strategy=ScriptedStrategy("btc", signals),
        symbol="BTCUSDT",
        interval="1h",
        rows=make_rows(["90", "100", "120"]),
    )
    eth = PortfolioBacktestInput(
        strategy=ScriptedStrategy("eth", signals),
        symbol="ETHUSDT",
        interval="1h",
        rows=make_rows(["45", "50", "40"]),
    )

    report = engine.run_portfolio([btc, eth])

    assert report.final_quote_balance == Decimal("250.00")
    values = {item.instrument.symbol: item for item in report.positions}
    assert values["BTCUSDT"].quantity == Decimal("5.0")
    assert values["BTCUSDT"].market_value == Decimal("600.0")
    assert values["ETHUSDT"].quantity == Decimal("5.00")
    assert values["ETHUSDT"].market_value == Decimal("200.00")
    assert report.final_equity == Decimal("1050.00")
    assert report.realized_pnl == 0
    assert report.unrealized_pnl == Decimal("50.00")
    assert report.total_pnl == report.realized_pnl + report.unrealized_pnl


def test_multi_symbol_rejects_duplicate_or_misaligned_streams() -> None:
    engine = BacktestEngine()
    btc = input_for("BTCUSDT", ["90", "100", "110"])
    duplicate = input_for("BTCUSDT", ["90", "100", "110"])
    misaligned = PortfolioBacktestInput(
        strategy=ScriptedStrategy(
            "eth",
            [Signal.BUY, Signal.SELL, Signal.HOLD],
        ),
        symbol="ETHUSDT",
        interval="1h",
        rows=make_rows(["45", "50", "55"], offset=HOUR),
    )

    with pytest.raises(ValueError, match="one strategy stream"):
        engine.run_portfolio([btc, duplicate])
    with pytest.raises(ValueError, match="identical timelines"):
        engine.run_portfolio([btc, misaligned])
