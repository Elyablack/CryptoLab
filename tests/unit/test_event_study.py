from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

from cryptolab.features.models import FeatureRow
from cryptolab.market_data.models import Candle
from cryptolab.research.event_study import (
    SignalEventStudyRunner,
    _bootstrap_interval,
    _forward_values,
    _overlap_cluster_means,
)
from cryptolab.research.event_study_formatter import (
    signal_event_study_to_csv,
    signal_event_study_to_json,
)
from cryptolab.strategies import Signal
from cryptolab.strategies.base import make_decision


HOUR = 3_600_000


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
            volume=Decimal("10") + index,
            close_time=open_time + HOUR - 1,
            quote_volume=Decimal("1000") + index * 100,
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ))
    return result


class ScriptedStrategy:
    name = "scripted"
    minimum_history = 1

    def __init__(self, signals: dict[int, Signal]) -> None:
        self._signals = signals

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: Sequence[FeatureRow],
    ):
        index = len(history) - 1
        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=history[-1],
            signal=self._signals.get(index, Signal.HOLD),
            explanation="scripted event",
        )


def get_summary(report, side: Signal, period: str, horizon: int):
    return next(
        item
        for item in report.summaries
        if item.strategy_name == "scripted"
        and item.side is side
        and item.period == period
        and item.horizon_hours == horizon
    )


def test_next_open_forward_returns_and_buy_sell_excursions() -> None:
    candles = make_candles(120)
    candles[11] = replace(
        candles[11],
        open=Decimal("100"),
        high=Decimal("115"),
        low=Decimal("90"),
        close=Decimal("110"),
        quote_volume=Decimal("1100"),
    )
    candles[21] = replace(
        candles[21],
        open=Decimal("100"),
        high=Decimal("108"),
        low=Decimal("85"),
        close=Decimal("90"),
        quote_volume=Decimal("900"),
    )
    report = SignalEventStudyRunner().run(
        "BTCUSDT",
        "1h",
        candles,
        strategies=(ScriptedStrategy({10: Signal.BUY, 20: Signal.SELL}),),
        horizons_hours=(1,),
        as_of_time=120 * HOUR,
        bootstrap_samples=20,
        minimum_independent_events=2,
    )

    buy = get_summary(report, Signal.BUY, "train", 1)
    sell = get_summary(report, Signal.SELL, "train", 1)
    assert buy.mean_market_return == Decimal("0.1")
    assert buy.mean_directional_return == Decimal("0.1")
    assert buy.mean_mae == Decimal("-0.1")
    assert buy.mean_mfe == Decimal("0.15")
    assert sell.mean_market_return == Decimal("-0.1")
    assert sell.mean_directional_return == Decimal("0.1")
    assert sell.mean_mae == Decimal("-0.08")
    assert sell.mean_mfe == Decimal("0.15")
    assert buy.conclusion == "inconclusive_insufficient_independent_events"
    buy_event = next(
        item
        for item in report.observations
        if item.side is Signal.BUY and item.horizon_hours == 1
    )
    assert buy_event.signal_candle_open_time == 10 * HOUR
    assert buy_event.signal_available_time == 11 * HOUR
    assert buy_event.entry_open_time == 11 * HOUR
    assert buy_event.entry_price == Decimal("100")
    assert buy_event.exit_close_time == 12 * HOUR - 1
    assert buy_event.exit_price == Decimal("110")


def test_forward_window_uses_entry_candle_through_horizon_close() -> None:
    candles = make_candles(120)
    candles[11] = replace(
        candles[11], high=Decimal("104"), low=Decimal("98")
    )
    candles[12] = replace(
        candles[12], high=Decimal("120"), low=Decimal("80")
    )
    rows = [
        FeatureRow(
            open_time=item.open_time,
            close_time=item.close_time,
            available_time=item.close_time + 1,
            open=item.open,
            high=item.high,
            low=item.low,
            close=item.close,
            volume=item.volume,
            simple_return=None,
            log_return=None,
            sma20=None,
            sma50=None,
            ema20=None,
            rsi14=None,
            atr14=None,
            rvol20=None,
        )
        for item in candles
    ]

    one_hour = _forward_values(rows, 11, 1, Signal.BUY)
    two_hours = _forward_values(rows, 11, 2, Signal.BUY)
    assert one_hour[2:] == (Decimal("-0.02"), Decimal("0.04"))
    assert two_hours[2:] == (Decimal("-0.2"), Decimal("0.2"))


def test_horizons_do_not_cross_chronological_period_boundaries() -> None:
    report = SignalEventStudyRunner().run(
        "BTCUSDT",
        "1h",
        make_candles(120),
        strategies=(ScriptedStrategy({70: Signal.BUY, 71: Signal.BUY}),),
        horizons_hours=(1, 6),
        as_of_time=120 * HOUR,
        bootstrap_samples=10,
    )

    train_count = next(
        item
        for item in report.signal_counts
        if item.side is Signal.BUY and item.period == "train"
    )
    assert train_count.decisions == 2
    assert train_count.entry_eligible == 1
    assert get_summary(report, Signal.BUY, "train", 1).signal_count == 1
    assert get_summary(report, Signal.BUY, "train", 6).signal_count == 0


def test_future_changes_affect_outcomes_but_not_prior_signal_generation() -> None:
    candles = make_candles(120)
    arguments = dict(
        symbol="BTCUSDT",
        interval="1h",
        strategies=(ScriptedStrategy({10: Signal.BUY}),),
        horizons_hours=(1,),
        as_of_time=120 * HOUR,
        bootstrap_samples=10,
    )
    first = SignalEventStudyRunner().run(candles=candles, **arguments)
    candles[11] = replace(
        candles[11],
        high=Decimal("121"),
        low=Decimal("99"),
        close=Decimal("120"),
        quote_volume=Decimal("1200"),
    )
    second = SignalEventStudyRunner().run(candles=candles, **arguments)

    assert first.signal_counts == second.signal_counts
    assert get_summary(first, Signal.BUY, "train", 1).signal_count == 1
    assert (
        get_summary(first, Signal.BUY, "train", 1).mean_directional_return
        != get_summary(second, Signal.BUY, "train", 1).mean_directional_return
    )


def test_matched_controls_and_exports_are_deterministic() -> None:
    candles = make_candles(120)
    strategy = ScriptedStrategy({10: Signal.BUY, 30: Signal.BUY, 50: Signal.BUY})
    runner = SignalEventStudyRunner()
    arguments = dict(
        symbol="BTCUSDT",
        interval="1h",
        candles=candles,
        strategies=(strategy,),
        horizons_hours=(1, 6),
        as_of_time=120 * HOUR,
        random_seed=42,
        bootstrap_samples=20,
        minimum_independent_events=2,
    )

    first = runner.run(**arguments)
    second = runner.run(**arguments)
    assert first == second
    assert signal_event_study_to_json(first) == signal_event_study_to_json(second)
    csv_output = signal_event_study_to_csv(first)
    assert "row_type,strategy,side,period" in csv_output
    assert "horizon_summary,scripted,BUY,train,1" in csv_output
    assert "event_observation,scripted,BUY,train,1" in csv_output
    assert '"next_candle_open"' in signal_event_study_to_json(first)


def test_overlapping_events_form_temporal_clusters() -> None:
    edges = [
        (0, Decimal("0.01")),
        (HOUR, Decimal("0.02")),
        (7 * HOUR, Decimal("-0.01")),
    ]

    clusters = _overlap_cluster_means(edges, 6 * HOUR)

    assert clusters == [Decimal("0.015"), Decimal("-0.01")]


def test_cluster_bootstrap_preserves_event_weighting_inside_cluster() -> None:
    cluster = [[Decimal("0.1"), Decimal("0.1"), Decimal("-0.1")]]

    lower, upper = _bootstrap_interval(cluster, 20, 42)

    expected = Decimal("0.1") / Decimal("3")
    assert lower == expected
    assert upper == expected
