from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    FixedFractionPositionSizer,
)
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.models import Candle
from cryptolab.replay import (
    DeterministicReplayRunner,
    ReplayEventType,
    ReplayRiskContext,
    RiskCheckResult,
)
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal
from cryptolab.strategies.runner import StrategyRunner


HOUR = 3_600_000


def make_candles(
    opens: Sequence[str] = ("100", "110", "120", "115", "105"),
    closes: Sequence[str] = ("102", "111", "119", "114", "106"),
) -> list[Candle]:
    candles = []
    for index, (open_value, close_value) in enumerate(
        zip(opens, closes, strict=True)
    ):
        open_price = Decimal(open_value)
        close_price = Decimal(close_value)
        high = max(open_price, close_price) + Decimal("2")
        low = min(open_price, close_price) - Decimal("2")
        candles.append(Candle(
            open_time=index * HOUR,
            open=open_price,
            high=high,
            low=low,
            close=close_price,
            volume=Decimal("10") + index,
            close_time=(index + 1) * HOUR - 1,
            quote_volume=(Decimal("10") + index) * close_price,
            number_of_trades=100 + index,
            taker_buy_base_volume=Decimal("4"),
            taker_buy_quote_volume=Decimal("4") * close_price,
        ))
    return candles


class ScriptedStrategy:
    name = "replay-scripted"
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
            explanation="deterministic scripted decision",
        )


class HistoryAuditStrategy:
    name = "history-audit"

    def __init__(self, minimum_history: int = 1) -> None:
        self.minimum_history = minimum_history
        self.observed: list[tuple[int, tuple[int, ...]]] = []

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: Sequence[FeatureRow],
    ):
        row = history[-1]
        self.observed.append((
            row.available_time,
            tuple(item.open_time for item in history),
        ))
        signal = Signal.BUY if row.close > row.open else Signal.HOLD
        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=row,
            signal=signal,
            explanation="decision based on the latest closed row",
        )


class RejectBuyRiskPolicy:
    name = "reject_buy"

    def check(self, context: ReplayRiskContext) -> RiskCheckResult:
        if context.decision.signal is Signal.BUY:
            return RiskCheckResult(False, "BUY disabled by test risk policy")
        return RiskCheckResult(True, "non-BUY decision accepted")


@pytest.mark.parametrize(
    "position_sizer",
    [None, FixedFractionPositionSizer(Decimal("0.4"))],
)
def test_replay_matches_batch_backtest_exactly(position_sizer) -> None:
    candles = make_candles()
    rows = FeatureBuilder().build(
        candles,
        interval="1h",
        as_of_time=candles[-1].close_time + 1,
    )
    signals = [
        Signal.BUY,
        Signal.HOLD,
        Signal.SELL,
        Signal.SELL,
        Signal.HOLD,
    ]
    config = BacktestConfig(
        initial_cash=Decimal("1000"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.0005"),
    )
    engine = BacktestEngine(config, position_sizer=position_sizer)

    expected = engine.run(
        ScriptedStrategy(signals),
        "BTCUSDT",
        "1h",
        rows,
    )
    actual = DeterministicReplayRunner(
        config,
        position_sizer=position_sizer,
    ).run(
        ScriptedStrategy(signals),
        "BTCUSDT",
        "1h",
        iter(candles),
    )

    assert actual.backtest == expected
    assert actual.decisions == tuple(
        StrategyRunner([ScriptedStrategy(signals)]).run(
            "BTCUSDT",
            "1h",
            rows,
        )
    )
    assert actual.backtest.final_state.quote_balance >= 0
    assert actual.backtest.final_state.base_balance >= 0
    assert actual.backtest.fees_paid == sum(
        (fill.fee for fill in actual.backtest.fills),
        Decimal("0"),
    )
    assert actual.backtest.total_pnl == (
        actual.backtest.realized_pnl + actual.backtest.unrealized_pnl
    )


def test_replay_event_order_and_identifiers_are_deterministic() -> None:
    candles = make_candles()
    signals = [Signal.BUY, Signal.HOLD, Signal.SELL, Signal.HOLD, Signal.HOLD]
    runner = DeterministicReplayRunner()

    first = runner.run(
        ScriptedStrategy(signals),
        "BTCUSDT",
        "1h",
        candles,
    )
    second = runner.run(
        ScriptedStrategy(signals),
        "BTCUSDT",
        "1h",
        candles,
    )

    assert first == second
    assert [event.sequence for event in first.events] == list(
        range(1, len(first.events) + 1)
    )
    assert len({event.event_id for event in first.events}) == len(first.events)
    assert [event.occurred_at for event in first.events] == sorted(
        event.occurred_at for event in first.events
    )
    assert first.events[0].event_type is ReplayEventType.CANDLE_OPEN
    assert first.events[-1].event_type is ReplayEventType.PORTFOLIO_SNAPSHOT


def test_reprocessing_same_candle_event_is_idempotent() -> None:
    candles = make_candles()
    session = DeterministicReplayRunner().start(
        ScriptedStrategy([Signal.HOLD] * len(candles)),
        "BTCUSDT",
        "1h",
    )

    first = session.process_candle(candles[0], event_id="candle:one")
    event_count = len(session.events)
    snapshot_count = len(session.snapshots)
    decision_count = len(session.decisions)
    duplicate = session.process_candle(candles[0], event_id="candle:one")

    assert duplicate is first
    assert len(session.events) == event_count
    assert len(session.snapshots) == snapshot_count
    assert len(session.decisions) == decision_count

    with pytest.raises(ValueError, match="Conflicting duplicate"):
        session.process_candle(
            replace(candles[0], close=Decimal("101")),
            event_id="candle:one",
        )


def test_warmup_and_incremental_history_never_expose_future_rows() -> None:
    candles = make_candles()
    strategy = HistoryAuditStrategy(minimum_history=3)
    report = DeterministicReplayRunner().run(
        strategy,
        "BTCUSDT",
        "1h",
        candles,
    )

    assert [decision.signal for decision in report.decisions[:2]] == [
        Signal.HOLD,
        Signal.HOLD,
    ]
    assert [len(open_times) for _, open_times in strategy.observed] == [3, 4, 5]
    for available_time, open_times in strategy.observed:
        assert all(open_time < available_time for open_time in open_times)
        assert open_times[-1] + HOUR == available_time


def test_future_candle_change_cannot_change_prior_decisions() -> None:
    original = make_candles()
    changed = [
        *original[:-1],
        replace(
            original[-1],
            high=Decimal("205"),
            close=Decimal("200"),
            quote_volume=Decimal("2800"),
            taker_buy_quote_volume=Decimal("800"),
        ),
    ]
    first = DeterministicReplayRunner().run(
        HistoryAuditStrategy(),
        "BTCUSDT",
        "1h",
        original,
    )
    second = DeterministicReplayRunner().run(
        HistoryAuditStrategy(),
        "BTCUSDT",
        "1h",
        changed,
    )

    assert first.decisions[:-1] == second.decisions[:-1]
    assert first.backtest.fills == second.backtest.fills
    assert first.snapshots[:-1] == second.snapshots[:-1]


def test_risk_rejection_creates_no_intent_fill_or_balance_change() -> None:
    candles = make_candles(opens=("100", "101", "102"), closes=("100",) * 3)
    report = DeterministicReplayRunner(
        risk_policy=RejectBuyRiskPolicy(),
    ).run(
        ScriptedStrategy([Signal.BUY, Signal.SELL, Signal.HOLD]),
        "BTCUSDT",
        "1h",
        candles,
    )

    assert report.backtest.order_intents == ()
    assert report.backtest.orders == ()
    assert report.backtest.execution_fills == ()
    assert report.backtest.fills == ()
    assert report.backtest.final_state.quote_balance == Decimal("1000")
    assert [item.reason for item in report.backtest.skipped_decisions] == [
        "Risk check rejected decision: BUY disabled by test risk policy",
        "SELL ignored because the portfolio is flat",
    ]
    assert [item.decision.signal for item in report.backtest.skipped_decisions] == [
        Signal.BUY,
        Signal.SELL,
    ]


def test_replay_rejects_invalid_timeline_and_unsupported_exit_models() -> None:
    candles = make_candles()
    session = DeterministicReplayRunner().start(
        ScriptedStrategy([Signal.HOLD] * len(candles)),
        "BTCUSDT",
        "1h",
    )
    session.process_candle(candles[0])

    with pytest.raises(ValueError, match="consecutive"):
        session.process_candle(candles[2])

    with pytest.raises(ValueError, match="does not support stop"):
        DeterministicReplayRunner(
            BacktestConfig(trailing_stop_rate=Decimal("0.05")),
        ).start(
            ScriptedStrategy([Signal.HOLD]),
            "BTCUSDT",
            "1h",
        )


def test_last_actionable_decision_is_audited_without_execution() -> None:
    candles = make_candles(opens=("100", "101"), closes=("100", "101"))
    report = DeterministicReplayRunner().run(
        ScriptedStrategy([Signal.HOLD, Signal.BUY]),
        "BTCUSDT",
        "1h",
        candles,
    )

    assert report.backtest.fills == ()
    assert report.backtest.skipped_decisions[-1].decision.signal is Signal.BUY
    assert report.backtest.skipped_decisions[-1].reason == (
        "no next candle is available for execution"
    )
    assert report.events[-1].event_type is ReplayEventType.UNEXECUTED_DECISION

