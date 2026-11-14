from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    FixedFractionPositionSizer,
    FixedFractionScaleSizer,
    LegacyAllInAllOutSizer,
    SizingRejection,
)
from cryptolab.backtesting.formatter import format_backtest_report
from cryptolab.features.models import FeatureRow
from cryptolab.execution.models import IntentSource, OrderSide, OrderStatus
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal


HOUR_MILLISECONDS = 3_600_000


def make_rows(opens: list[str]) -> list[FeatureRow]:
    rows = []
    for index, value in enumerate(opens):
        open_time = index * HOUR_MILLISECONDS
        price = Decimal(value)
        rows.append(FeatureRow(
            open_time=open_time,
            close_time=open_time + HOUR_MILLISECONDS - 1,
            available_time=open_time + HOUR_MILLISECONDS,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
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
    name = "sizing_test"
    minimum_history = 1

    def __init__(self, signals: Sequence[Signal]) -> None:
        self._signals = tuple(signals)

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: Sequence[FeatureRow],
    ):
        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=history[-1],
            signal=self._signals[len(history) - 1],
            explanation="deterministic sizing test",
        )


def test_explicit_legacy_sizer_is_exactly_default_equivalent() -> None:
    rows = make_rows(["90", "100", "110"])
    strategy = ScriptedStrategy([Signal.BUY, Signal.SELL, Signal.HOLD])

    implicit = BacktestEngine().run(strategy, "BTCUSDT", "1h", rows)
    explicit = BacktestEngine(
        position_sizer=LegacyAllInAllOutSizer(),
    ).run(strategy, "BTCUSDT", "1h", rows)

    assert explicit == implicit
    assert format_backtest_report(explicit) == format_backtest_report(implicit)


def test_fractional_buy_preserves_cash_and_sell_remains_all_out() -> None:
    rows = make_rows(["90", "100", "110"])
    strategy = ScriptedStrategy([Signal.BUY, Signal.SELL, Signal.HOLD])
    engine = BacktestEngine(
        BacktestConfig(
            fee_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
        ),
        position_sizer=FixedFractionPositionSizer(Decimal("0.25")),
    )

    report = engine.run(strategy, "BTCUSDT", "1h", rows)

    assert report.fills[0].base_quantity == Decimal("2.5")
    assert report.fills[0].quote_notional == Decimal("250.00")
    assert report.fills[0].quote_balance_after == Decimal("750.00")
    assert report.fills[1].base_quantity == Decimal("2.5")
    assert report.final_state.base_balance == 0
    assert report.final_state.quote_balance == Decimal("1025.00")
    assert report.realized_pnl == Decimal("25.00")
    assert report.completed_trades[0].pnl == Decimal("25.00")
    assert report.total_pnl == report.realized_pnl + report.unrealized_pnl


def test_fractional_buy_fee_is_contained_in_allocated_budget() -> None:
    rows = make_rows(["90", "100"])
    strategy = ScriptedStrategy([Signal.BUY, Signal.HOLD])
    config = BacktestConfig(
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.0005"),
    )
    report = BacktestEngine(
        config,
        position_sizer=FixedFractionPositionSizer(Decimal("0.25")),
    ).run(strategy, "BTCUSDT", "1h", rows)

    budget = Decimal("250.00")
    notional = budget / Decimal("1.001")
    fee = budget - notional
    execution_price = Decimal("100") * Decimal("1.0005")

    assert report.final_state.quote_balance == Decimal("750.00")
    assert report.final_state.entry_cost == budget
    assert report.final_state.base_balance == notional / execution_price
    assert report.fills[0].fee == fee
    assert report.fills[0].quote_notional + report.fills[0].fee == budget
    assert report.final_state.quote_balance >= 0


def test_strategy_fills_are_linked_to_deterministic_order_intents() -> None:
    rows = make_rows(["90", "100", "110"])
    strategy = ScriptedStrategy([Signal.BUY, Signal.SELL, Signal.HOLD])
    engine = BacktestEngine(
        BacktestConfig(fee_rate=Decimal("0"), slippage_rate=Decimal("0")),
        position_sizer=FixedFractionPositionSizer(Decimal("0.25")),
    )

    first = engine.run(strategy, "BTCUSDT", "1h", rows, exchange="bybit")
    second = engine.run(strategy, "BTCUSDT", "1h", rows, exchange="bybit")

    assert first == second
    assert len(first.order_intents) == 2
    buy_intent, sell_intent = first.order_intents
    assert buy_intent.source is IntentSource.STRATEGY
    assert buy_intent.side is OrderSide.BUY
    assert sell_intent.side is OrderSide.SELL
    assert buy_intent.instrument.exchange == "bybit"
    assert buy_intent.instrument.base_asset == "BTC"
    assert buy_intent.instrument.quote_asset == "USDT"
    assert buy_intent.source_candle_open_time == rows[0].open_time
    assert buy_intent.signal_available_time == rows[0].available_time
    assert buy_intent.created_at == rows[1].open_time
    assert buy_intent.earliest_execution_at == rows[1].open_time
    assert buy_intent.quantity == first.fills[0].base_quantity
    assert sell_intent.quantity == first.fills[1].base_quantity
    assert [fill.intent_id for fill in first.fills] == [
        intent.intent_id for intent in first.order_intents
    ]
    assert len({intent.intent_id for intent in first.order_intents}) == 2
    assert len({intent.source_event_id for intent in first.order_intents}) == 2
    assert len(first.orders) == len(first.execution_fills) == 2
    assert all(order.status is OrderStatus.FILLED for order in first.orders)
    assert {order.intent_id for order in first.orders} == {
        intent.intent_id for intent in first.order_intents
    }
    assert {fill.fill_id for fill in first.execution_fills} == {
        fill.fill_id for fill in first.fills
    }

    with pytest.raises(FrozenInstanceError):
        buy_intent.quantity = Decimal("1")  # type: ignore[misc]


def test_repeated_buy_is_still_skipped_without_creating_an_intent() -> None:
    rows = make_rows(["90", "100", "105", "110"])
    strategy = ScriptedStrategy([
        Signal.BUY,
        Signal.BUY,
        Signal.SELL,
        Signal.HOLD,
    ])
    report = BacktestEngine(
        BacktestConfig(fee_rate=Decimal("0"), slippage_rate=Decimal("0")),
        position_sizer=FixedFractionPositionSizer(Decimal("0.25")),
    ).run(strategy, "BTCUSDT", "1h", rows)

    assert len(report.order_intents) == 2
    assert len(report.fills) == 2
    assert len(report.skipped_decisions) == 1
    assert report.skipped_decisions[0].reason == (
        "BUY ignored because the portfolio already holds the base asset"
    )


def test_sizing_rejection_is_auditable_and_creates_no_intent() -> None:
    class RejectingSizer:
        name = "reject_all"

        def size(self, context):
            del context
            return SizingRejection("configured research limit")

    rows = make_rows(["90", "100"])
    strategy = ScriptedStrategy([Signal.BUY, Signal.HOLD])

    report = BacktestEngine(
        position_sizer=RejectingSizer(),
    ).run(strategy, "BTCUSDT", "1h", rows)

    assert report.order_intents == ()
    assert report.fills == ()
    assert report.final_equity == Decimal("1000")
    assert report.skipped_decisions[0].reason == (
        "Position sizing rejected: configured research limit"
    )


@pytest.mark.parametrize(
    "fraction",
    [Decimal("0"), Decimal("-0.1"), Decimal("1.1"), Decimal("NaN")],
)
def test_fixed_fraction_sizer_rejects_invalid_fraction(
    fraction: Decimal,
) -> None:
    with pytest.raises(ValueError, match="buy_fraction"):
        FixedFractionPositionSizer(fraction)


def test_nondefault_sizer_is_explicit_in_formatted_report() -> None:
    report = BacktestEngine(
        position_sizer=FixedFractionPositionSizer(Decimal("0.25")),
    ).run(
        ScriptedStrategy([Signal.HOLD, Signal.HOLD]),
        "BTCUSDT",
        "1h",
        make_rows(["100", "100"]),
    )

    assert (
        "Position sizing:  fixed_fraction(buy_fraction=0.25)"
        in format_backtest_report(report)
    )


def test_scale_sizer_supports_multiple_entries_and_fifo_exit() -> None:
    rows = make_rows(["90", "100", "110", "120"])
    strategy = ScriptedStrategy([
        Signal.BUY,
        Signal.BUY,
        Signal.SELL,
        Signal.HOLD,
    ])
    report = BacktestEngine(
        BacktestConfig(fee_rate=Decimal("0"), slippage_rate=Decimal("0")),
        position_sizer=FixedFractionScaleSizer(
            buy_fraction=Decimal("0.5"),
            sell_fraction=Decimal("1"),
        ),
    ).run(strategy, "BTCUSDT", "1h", rows)

    assert [fill.side for fill in report.fills] == [
        Signal.BUY,
        Signal.BUY,
        Signal.SELL,
    ]
    assert report.fills[0].quote_notional == Decimal("500.0")
    assert report.fills[1].quote_notional == Decimal("250.00")
    assert report.final_state.base_balance == 0
    assert report.final_state.entry_cost == 0
    assert len(report.lot_realizations) == 2
    assert len(report.completed_trades) == 2
    assert report.position_cycles[0].entry_fill_ids == (
        report.fills[0].fill_id,
        report.fills[1].fill_id,
    )
    assert not report.position_cycles[0].is_open
    assert report.realized_pnl == sum(
        (trade.pnl for trade in report.completed_trades), Decimal("0")
    )


def test_scale_sizer_partial_sell_leaves_fifo_cost_basis_open() -> None:
    rows = make_rows(["90", "100", "120"])
    strategy = ScriptedStrategy([Signal.BUY, Signal.SELL, Signal.HOLD])
    report = BacktestEngine(
        BacktestConfig(fee_rate=Decimal("0"), slippage_rate=Decimal("0")),
        position_sizer=FixedFractionScaleSizer(
            buy_fraction=Decimal("0.5"),
            sell_fraction=Decimal("0.5"),
        ),
    ).run(strategy, "BTCUSDT", "1h", rows)

    assert report.final_state.quote_balance == Decimal("800.00")
    assert report.final_state.base_balance == Decimal("2.5")
    assert report.final_state.entry_cost == Decimal("250.00")
    assert report.realized_pnl == Decimal("50.00")
    assert report.unrealized_pnl == Decimal("50.00")
    assert report.total_pnl == Decimal("100.00")
    assert report.position_cycles[0].is_open
    assert report.completed_trades[0].closed_quantity == Decimal("2.5")
    assert report.completed_trades[0].allocated_entry_cost == Decimal("250.00")


def test_multi_lot_open_position_report_does_not_claim_one_entry_price() -> None:
    rows = make_rows(["90", "100", "110"])
    report = BacktestEngine(
        BacktestConfig(fee_rate=Decimal("0"), slippage_rate=Decimal("0")),
        position_sizer=FixedFractionScaleSizer(
            buy_fraction=Decimal("0.5"),
            sell_fraction=Decimal("1"),
        ),
    ).run(
        ScriptedStrategy([Signal.BUY, Signal.BUY, Signal.HOLD]),
        "BTCUSDT",
        "1h",
        rows,
    )

    output = format_backtest_report(report, show_trades=True)

    assert "Open FIFO lots: 2" in output
    assert "Average cost including entry fees:" in output
    assert "Entry price:" not in output


@pytest.mark.parametrize(
    ("buy_fraction", "sell_fraction"),
    [
        (Decimal("0"), Decimal("1")),
        (Decimal("1"), Decimal("0")),
        (Decimal("1.1"), Decimal("1")),
        (Decimal("1"), Decimal("NaN")),
    ],
)
def test_scale_sizer_rejects_invalid_fractions(
    buy_fraction: Decimal,
    sell_fraction: Decimal,
) -> None:
    with pytest.raises(ValueError, match="fraction"):
        FixedFractionScaleSizer(buy_fraction, sell_fraction)
