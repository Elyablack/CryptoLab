from collections.abc import Sequence
from decimal import Decimal

import pytest

from cryptolab.backtesting import BacktestConfig, BacktestEngine
from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal


HOUR_MILLISECONDS = 3_600_000


def make_rows(
    opens: list[str],
    closes: list[str] | None = None,
) -> list[FeatureRow]:
    close_values = closes or opens
    rows = []
    for index, (open_value, close_value) in enumerate(
        zip(opens, close_values, strict=True)
    ):
        open_time = index * HOUR_MILLISECONDS
        open_price = Decimal(open_value)
        close_price = Decimal(close_value)
        rows.append(FeatureRow(
            open_time=open_time,
            close_time=open_time + HOUR_MILLISECONDS - 1,
            available_time=open_time + HOUR_MILLISECONDS,
            open=open_price,
            high=max(open_price, close_price) + Decimal("1"),
            low=min(open_price, close_price) - Decimal("1"),
            close=close_price,
            volume=Decimal("10"),
            simple_return=Decimal("0"),
            log_return=Decimal("0"),
            sma20=Decimal("100"),
            sma50=Decimal("100"),
            ema20=Decimal("100"),
            rsi14=Decimal("50"),
            atr14=Decimal("2"),
            rvol20=Decimal("1"),
        ))
    return rows


class ScriptedStrategy:
    name = "scripted"
    minimum_history = 1

    def __init__(self, signals: Sequence[Signal]) -> None:
        self._signals = tuple(signals)

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: Sequence[FeatureRow],
    ):
        row = history[-1]
        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=row,
            signal=self._signals[len(history) - 1],
            explanation="scripted test decision",
        )


def test_buy_hold_sell_executes_at_next_open_with_fees_and_slippage() -> None:
    rows = make_rows(["100", "110", "120", "130"])
    strategy = ScriptedStrategy([
        Signal.BUY,
        Signal.HOLD,
        Signal.SELL,
        Signal.HOLD,
    ])

    report = BacktestEngine().run(strategy, "BTCUSDT", "1h", rows)

    buy_notional = Decimal("1000") / Decimal("1.001")
    buy_fee = Decimal("1000") - buy_notional
    buy_price = Decimal("110") * Decimal("1.0005")
    quantity = buy_notional / buy_price
    sell_price = Decimal("130") * Decimal("0.9995")
    sell_notional = quantity * sell_price
    sell_fee = sell_notional * Decimal("0.001")
    final_cash = sell_notional - sell_fee
    expected_pnl = final_cash - Decimal("1000")

    assert [fill.side for fill in report.fills] == [Signal.BUY, Signal.SELL]
    assert report.fills[0].execution_open_time == rows[1].open_time
    assert report.fills[0].execution_price == buy_price
    assert report.fills[1].execution_open_time == rows[3].open_time
    assert report.fills[1].execution_price == sell_price
    assert report.final_state.quote_balance == final_cash
    assert report.final_state.base_balance == 0
    assert report.final_state.fees_paid == buy_fee + sell_fee
    assert report.realized_pnl == expected_pnl
    assert report.unrealized_pnl == 0
    assert report.total_pnl == expected_pnl
    assert len(report.completed_trades) == 1
    assert report.completed_trades[0].pnl == expected_pnl
    assert report.win_rate == Decimal("1")


def test_invalid_position_transitions_are_ignored_with_reasons() -> None:
    rows = make_rows(["100"] * 6)
    strategy = ScriptedStrategy([
        Signal.SELL,
        Signal.BUY,
        Signal.BUY,
        Signal.SELL,
        Signal.SELL,
        Signal.HOLD,
    ])

    report = BacktestEngine().run(strategy, "BTCUSDT", "1h", rows)

    assert [fill.side for fill in report.fills] == [Signal.BUY, Signal.SELL]
    assert [item.reason for item in report.skipped_decisions] == [
        "SELL ignored because the portfolio is flat",
        "BUY ignored because the portfolio already holds the base asset",
        "SELL ignored because the portfolio is flat",
    ]
    assert report.final_state.quote_balance >= 0
    assert report.final_state.base_balance == 0
    assert all(point.equity >= 0 for point in report.equity_curve)


def test_last_actionable_signal_is_reported_without_inventing_fill() -> None:
    rows = make_rows(["100", "110"])
    strategy = ScriptedStrategy([Signal.HOLD, Signal.BUY])

    report = BacktestEngine().run(strategy, "BTCUSDT", "1h", rows)

    assert report.fills == ()
    assert len(report.skipped_decisions) == 1
    assert report.skipped_decisions[0].decision.signal is Signal.BUY
    assert report.skipped_decisions[0].reason == (
        "no next candle is available for execution"
    )
    assert report.final_equity == Decimal("1000")


def test_open_position_is_marked_at_last_close_without_final_sell() -> None:
    rows = make_rows(["100", "100"], closes=["100", "120"])
    strategy = ScriptedStrategy([Signal.BUY, Signal.HOLD])
    config = BacktestConfig(fee_rate=Decimal("0.002"))

    report = BacktestEngine(config).run(strategy, "BTCUSDT", "1h", rows)

    buy_notional = Decimal("1000") / Decimal("1.002")
    quantity = buy_notional / Decimal("100.05")
    expected_equity = quantity * Decimal("120")

    assert len(report.fills) == 1
    assert report.completed_trades == ()
    assert report.final_state.base_balance == quantity
    assert report.final_state.quote_balance == 0
    assert report.final_equity == expected_equity
    assert report.realized_pnl == 0
    assert report.unrealized_pnl == expected_equity - Decimal("1000")
    assert report.total_pnl == report.realized_pnl + report.unrealized_pnl
    assert report.win_rate is None


def test_equity_drawdown_is_non_positive_and_run_is_deterministic() -> None:
    rows = make_rows(["100", "100", "100"], closes=["100", "80", "70"])
    strategy = ScriptedStrategy([Signal.BUY, Signal.HOLD, Signal.HOLD])
    engine = BacktestEngine()

    first = engine.run(strategy, "BTCUSDT", "1h", rows)
    second = engine.run(strategy, "BTCUSDT", "1h", rows)

    assert first == second
    assert first.maximum_equity_drawdown < 0
    assert first.maximum_equity_drawdown == (
        first.final_equity - Decimal("1000")
    ) / Decimal("1000")


def test_losing_completed_trade_has_zero_win_rate() -> None:
    rows = make_rows(["100", "100", "90", "80"])
    strategy = ScriptedStrategy([
        Signal.BUY,
        Signal.HOLD,
        Signal.SELL,
        Signal.HOLD,
    ])

    report = BacktestEngine().run(strategy, "BTCUSDT", "1h", rows)

    assert report.completed_trade_count == 1
    assert report.completed_trades[0].pnl < 0
    assert report.win_rate == Decimal("0")
    assert report.realized_pnl == report.total_pnl


@pytest.mark.parametrize(
    "config",
    [
        {"initial_cash": Decimal("0")},
        {"fee_rate": Decimal("-0.001")},
        {"fee_rate": Decimal("1")},
        {"slippage_rate": Decimal("1")},
    ],
)
def test_backtest_config_rejects_invalid_accounting_parameters(
    config: dict[str, Decimal],
) -> None:
    with pytest.raises(ValueError):
        BacktestConfig(**config)
