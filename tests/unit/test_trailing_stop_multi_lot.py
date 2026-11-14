from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    FixedFractionScaleSizer,
    SimulatedFill,
    TrailingStopFill,
    TrailingStopTriggerKind,
)
from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal


HOUR = 3_600_000


def make_rows(prices: list[str]) -> list[FeatureRow]:
    result = []
    for index, value in enumerate(prices):
        price = Decimal(value)
        open_time = index * HOUR
        result.append(FeatureRow(
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
    return result


class ScriptedStrategy:
    name = "multi_lot_trailing"
    minimum_history = 1

    def __init__(self, signals: Sequence[Signal]) -> None:
        self._signals = tuple(signals)

    def evaluate(self, symbol, interval, history):
        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=history[-1],
            signal=self._signals[len(history) - 1],
            explanation="deterministic trailing-stop test",
        )


def engine() -> BacktestEngine:
    return BacktestEngine(
        BacktestConfig(
            fee_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            trailing_stop_rate=Decimal("0.10"),
        ),
        position_sizer=FixedFractionScaleSizer(
            buy_fraction=Decimal("0.5"),
            sell_fraction=Decimal("0.5"),
        ),
    )


def test_scale_in_preserves_existing_high_watermark() -> None:
    rows = make_rows(["90", "100", "115"])
    rows[1] = replace(rows[1], high=Decimal("120"), low=Decimal("99"))
    rows[2] = replace(rows[2], high=Decimal("116"), low=Decimal("107"))

    report = engine().run(
        ScriptedStrategy([Signal.BUY, Signal.BUY, Signal.HOLD]),
        "BTCUSDT",
        "1h",
        rows,
    )

    assert [fill.side for fill in report.fills] == [
        Signal.BUY,
        Signal.BUY,
        Signal.SELL,
    ]
    stop = report.fills[-1]
    assert isinstance(stop, TrailingStopFill)
    assert stop.trigger_kind is TrailingStopTriggerKind.INTRABAR
    assert stop.high_watermark == Decimal("120")
    assert stop.active_stop_price == Decimal("108.00")
    assert stop.base_quantity == (
        report.fills[0].base_quantity + report.fills[1].base_quantity
    )
    assert len(report.lot_realizations) == 2
    assert report.final_state.base_balance == 0
    assert report.position_cycles[0].closed_at == rows[2].open_time


def test_partial_strategy_sell_has_open_priority_then_stop_closes_remainder() -> None:
    rows = make_rows(["90", "100", "115"])
    rows[1] = replace(rows[1], high=Decimal("120"), low=Decimal("99"))
    rows[2] = replace(rows[2], high=Decimal("116"), low=Decimal("107"))

    report = engine().run(
        ScriptedStrategy([Signal.BUY, Signal.SELL, Signal.HOLD]),
        "BTCUSDT",
        "1h",
        rows,
    )

    assert len(report.fills) == 3
    partial_sell = report.fills[1]
    stop = report.fills[2]
    assert isinstance(partial_sell, SimulatedFill)
    assert partial_sell.side is Signal.SELL
    assert isinstance(stop, TrailingStopFill)
    assert stop.high_watermark == Decimal("120")
    assert stop.active_stop_price == Decimal("108.00")
    assert partial_sell.execution_open_time == stop.trigger_candle_open_time
    assert partial_sell.base_quantity + stop.base_quantity == (
        report.fills[0].base_quantity
    )
    assert report.final_state.base_balance == 0
    assert len(report.order_intents) == 3
    stop_intent = report.order_intents[-1]
    assert stop_intent.signal_available_time is None
    assert stop.intent_id == stop_intent.intent_id
    assert stop.fill_id == f"{stop_intent.intent_id}:fill:1"


def test_partial_exit_without_stop_preserves_and_advances_watermark() -> None:
    rows = make_rows(["90", "100", "115", "125"])
    rows[1] = replace(rows[1], high=Decimal("120"), low=Decimal("99"))
    rows[2] = replace(rows[2], high=Decimal("130"), low=Decimal("110"))
    rows[3] = replace(rows[3], high=Decimal("126"), low=Decimal("116"))

    report = engine().run(
        ScriptedStrategy([
            Signal.BUY,
            Signal.SELL,
            Signal.HOLD,
            Signal.HOLD,
        ]),
        "BTCUSDT",
        "1h",
        rows,
    )

    stop = report.fills[-1]
    assert isinstance(stop, TrailingStopFill)
    assert stop.trigger_candle_open_time == rows[3].open_time
    assert stop.high_watermark == Decimal("130")
    assert stop.active_stop_price == Decimal("117.0")
    assert report.final_state.base_balance == 0


def test_reentry_after_stop_starts_a_new_position_cycle() -> None:
    rows = make_rows(["90", "100", "90", "80"])
    rows[1] = replace(rows[1], high=Decimal("110"), low=Decimal("99"))
    rows[2] = replace(rows[2], high=Decimal("91"), low=Decimal("89"))
    rows[3] = replace(rows[3], high=Decimal("82"), low=Decimal("79"))

    report = engine().run(
        ScriptedStrategy([
            Signal.BUY,
            Signal.HOLD,
            Signal.BUY,
            Signal.HOLD,
        ]),
        "BTCUSDT",
        "1h",
        rows,
    )

    assert isinstance(report.fills[1], TrailingStopFill)
    assert isinstance(report.fills[2], SimulatedFill)
    assert report.fills[2].side is Signal.BUY
    assert len(report.position_cycles) == 2
    assert not report.position_cycles[0].is_open
    assert report.position_cycles[1].is_open
    assert report.open_lots[0].entry_fill_id == report.fills[2].fill_id
