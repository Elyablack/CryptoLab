import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest

from cryptolab.backtesting import BacktestConfig
from cryptolab.features.models import FeatureRow
from cryptolab.execution import (
    FakeVenueExecutionAdapter,
    Instrument,
    IntentSource,
    OrderIntent,
    OrderSide,
    OrderStatus,
    SimulatedExecutionAdapter,
)
from cryptolab.market_data.models import Candle
from cryptolab.replay import (
    CrashPoint,
    DeterministicReplayRunner,
    InjectedReplayCrash,
    PersistentReplayRunner,
    ReplayConfigurationError,
    ReplayRiskContext,
    ReplaySessionUnavailableError,
    ReplayStateIntegrityError,
    RiskCheckResult,
)
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal
from cryptolab.replay.serialization import (
    deserialize_execution_snapshot,
    serialize_execution_snapshot,
)


HOUR = 3_600_000


def make_candles() -> list[Candle]:
    opens = ("100", "110", "120", "115", "105")
    closes = ("102", "111", "119", "114", "106")
    candles = []
    for index, (open_value, close_value) in enumerate(
        zip(opens, closes, strict=True)
    ):
        open_price = Decimal(open_value)
        close_price = Decimal(close_value)
        volume = Decimal("10") + index
        candles.append(Candle(
            open_time=index * HOUR,
            open=open_price,
            high=max(open_price, close_price) + Decimal("2"),
            low=min(open_price, close_price) - Decimal("2"),
            close=close_price,
            volume=volume,
            close_time=(index + 1) * HOUR - 1,
            quote_volume=volume * close_price,
            number_of_trades=100 + index,
            taker_buy_base_volume=Decimal("4"),
            taker_buy_quote_volume=Decimal("4") * close_price,
        ))
    return candles


@dataclass(frozen=True)
class ScriptedStrategy:
    signals: tuple[Signal, ...]
    name: ClassVar[str] = "persistent-scripted"

    @property
    def minimum_history(self) -> int:
        return 1

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
            signal=self.signals[len(history) - 1],
            explanation="persistent replay test decision",
        )


class CountingRiskPolicy:
    name = "counting-risk"

    def __init__(self, maximum_checks: int = 100) -> None:
        self.maximum_checks = maximum_checks
        self.checks = 0

    def persistence_configuration(self) -> Mapping[str, object]:
        return {"maximum_checks": self.maximum_checks}

    def snapshot_state(self) -> Mapping[str, object]:
        return {"checks": self.checks}

    def restore_state(self, state: Mapping[str, object]) -> None:
        checks = state.get("checks")
        if type(checks) is not int or checks < 0:
            raise ValueError("checks must be a non-negative integer")
        self.checks = checks

    def check(self, context: ReplayRiskContext) -> RiskCheckResult:
        self.checks += 1
        accepted = self.checks <= self.maximum_checks
        return RiskCheckResult(
            accepted,
            "check limit not exceeded" if accepted else "check limit exceeded",
        )


SIGNALS = (
    Signal.BUY,
    Signal.HOLD,
    Signal.SELL,
    Signal.SELL,
    Signal.HOLD,
)


def strategy() -> ScriptedStrategy:
    return ScriptedStrategy(SIGNALS)


def persistent_runner(
    database: Path,
    *,
    config: BacktestConfig | None = None,
    risk_policy=None,
) -> PersistentReplayRunner:
    return PersistentReplayRunner(
        database,
        config,
        risk_policy=risk_policy,
    )


def test_continuous_replay_equals_interrupted_and_recovered_replay(
    tmp_path: Path,
) -> None:
    candles = make_candles()
    database = tmp_path / "paper-state.db"

    continuous = persistent_runner(database).create(
        "continuous",
        strategy(),
        "BTCUSDT",
        "1h",
        exchange="bybit",
    )
    for candle in candles:
        continuous.process_candle(candle)
    continuous_report = continuous.finish()

    interrupted = persistent_runner(database).create(
        "interrupted",
        strategy(),
        "BTCUSDT",
        "1h",
        exchange="bybit",
    )
    for candle in candles[:2]:
        interrupted.process_candle(candle)
    interrupted.close()

    recovered = persistent_runner(database).recover(
        "interrupted",
        strategy(),
        "BTCUSDT",
        "1h",
        exchange="bybit",
    )
    for candle in candles[2:]:
        recovered.process_candle(candle)
    recovered_report = recovered.finish()

    assert recovered_report == continuous_report
    assert recovered.current_state.portfolio == continuous.current_state.portfolio
    assert recovered.current_state.open_lots == continuous.current_state.open_lots
    assert recovered.current_state.checkpoint_sequence == len(candles)
    assert recovered_report.backtest.order_intents == (
        continuous_report.backtest.order_intents
    )
    assert recovered_report.backtest.orders == continuous_report.backtest.orders
    assert recovered_report.backtest.execution_fills == (
        continuous_report.backtest.execution_fills
    )


@pytest.mark.parametrize("crash_point", list(CrashPoint))
def test_crash_injection_recovers_exactly(
    tmp_path: Path,
    crash_point: CrashPoint,
) -> None:
    candles = make_candles()
    expected = DeterministicReplayRunner().run(
        strategy(),
        "BTCUSDT",
        "1h",
        candles,
        exchange="bybit",
    )
    database = tmp_path / f"{crash_point.value}.db"
    failed = persistent_runner(database).create(
        "crash-test",
        strategy(),
        "BTCUSDT",
        "1h",
        exchange="bybit",
    )
    failed.process_candle(candles[0])

    with pytest.raises(InjectedReplayCrash) as error:
        failed.process_candle(candles[1], crash_at=crash_point)
    assert error.value.point is crash_point
    with pytest.raises(ReplaySessionUnavailableError, match="fail-closed"):
        failed.process_candle(candles[1])

    recovered = persistent_runner(database).recover(
        "crash-test",
        strategy(),
        "BTCUSDT",
        "1h",
        exchange="bybit",
    )
    expected_committed = 2 if crash_point is CrashPoint.AFTER_COMMIT else 1
    assert recovered.current_state.checkpoint_sequence == expected_committed

    # Always retry the uncertain event. A post-commit retry is a persistent
    # no-op, while every pre-commit crash processes it exactly once.
    recovered.process_candle(candles[1])
    for candle in candles[2:]:
        recovered.process_candle(candle)

    assert recovered.finish() == expected
    state = recovered.current_state
    assert state.order_intent_count == len(expected.backtest.order_intents)
    assert state.order_count == len(expected.backtest.orders)
    assert state.fill_count == len(expected.backtest.execution_fills)
    assert state.quote_balance == expected.backtest.final_state.quote_balance
    assert state.base_balance == expected.backtest.final_state.base_balance
    assert state.realized_pnl == expected.backtest.realized_pnl
    assert state.fees_paid == expected.backtest.fees_paid


def test_duplicate_event_is_persistent_noop_and_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    candles = make_candles()
    database = tmp_path / "duplicates.db"
    session = persistent_runner(database).create(
        "duplicates",
        strategy(),
        "BTCUSDT",
        "1h",
    )
    first = session.process_candle(candles[0], event_id="stable-event")
    before = session.current_state
    repeated = session.process_candle(candles[0], event_id="stable-event")

    assert repeated is first
    assert session.current_state == before

    with pytest.raises(ReplayStateIntegrityError, match="Conflicting duplicate"):
        session.process_candle(
            replace(candles[0], close=Decimal("101")),
            event_id="stable-event",
        )
    with pytest.raises(ReplaySessionUnavailableError, match="fail-closed"):
        _ = session.current_state

    recovered = persistent_runner(database).recover(
        "duplicates",
        strategy(),
        "BTCUSDT",
        "1h",
    )
    assert recovered.current_state.checkpoint_sequence == 1
    assert recovered.current_state.order_count == 0


def test_stateful_risk_policy_is_reconstructed_from_initial_state(
    tmp_path: Path,
) -> None:
    candles = make_candles()
    database = tmp_path / "risk.db"
    original_policy = CountingRiskPolicy()
    session = persistent_runner(
        database,
        risk_policy=original_policy,
    ).create("risk", strategy(), "BTCUSDT", "1h")
    for candle in candles[:3]:
        session.process_candle(candle)
    assert original_policy.checks == 2
    session.close()

    recovered_policy = CountingRiskPolicy()
    recovered = persistent_runner(
        database,
        risk_policy=recovered_policy,
    ).recover("risk", strategy(), "BTCUSDT", "1h")

    assert recovered_policy.checks == 2
    assert '"checks":2' in recovered.current_state.risk_state_json
    for candle in candles[3:]:
        recovered.process_candle(candle)
    assert recovered_policy.checks == 4


def test_recovery_rejects_configuration_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "config.db"
    session = persistent_runner(database).create(
        "config",
        strategy(),
        "BTCUSDT",
        "1h",
    )
    session.process_candle(make_candles()[0])
    session.close()

    changed = BacktestConfig(fee_rate=Decimal("0.002"))
    with pytest.raises(ReplayConfigurationError, match="incompatible"):
        persistent_runner(database, config=changed).recover(
            "config",
            strategy(),
            "BTCUSDT",
            "1h",
        )


def test_recovery_fails_closed_on_corrupt_normalized_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corrupt.db"
    session = persistent_runner(database).create(
        "corrupt",
        strategy(),
        "BTCUSDT",
        "1h",
    )
    for candle in make_candles()[:2]:
        session.process_candle(candle)
    session.close()

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE paper_orders SET payload = '{"corrupt":true}'
            WHERE session_id = 'corrupt'
            """
        )

    with pytest.raises(ReplayStateIntegrityError, match="order state"):
        persistent_runner(database).recover(
            "corrupt",
            strategy(),
            "BTCUSDT",
            "1h",
        )


def test_schema_is_versioned_and_decimal_payloads_are_strings(
    tmp_path: Path,
) -> None:
    database = tmp_path / "schema.db"
    session = persistent_runner(database).create(
        "schema",
        strategy(),
        "BTCUSDT",
        "1h",
    )
    session.process_candle(make_candles()[0])

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        checkpoint = connection.execute(
            "SELECT checkpoint_json FROM paper_checkpoints"
        ).fetchone()[0]
        assert '"$decimal":"1000"' in checkpoint
        assert "1000.0" not in checkpoint


def test_newer_or_unversioned_schema_is_rejected(tmp_path: Path) -> None:
    newer = tmp_path / "newer.db"
    with sqlite3.connect(newer) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(ReplayStateIntegrityError, match="version 99"):
        persistent_runner(newer)

    unversioned = tmp_path / "unversioned.db"
    with sqlite3.connect(unversioned) as connection:
        connection.execute("CREATE TABLE unexpected (value TEXT)")
    with pytest.raises(ReplayStateIntegrityError, match="unversioned"):
        persistent_runner(unversioned)


def test_partial_execution_snapshot_survives_exact_serialization() -> None:
    adapter = _submitted_fake_adapter()
    adapter.process_fill_event(
        event_id="partial-1",
        order_id="paper-order",
        quantity=Decimal("4"),
        price=Decimal("100"),
        quote_notional=Decimal("400"),
        fee=Decimal("0.4"),
        fee_asset="USDT",
        executed_at=HOUR + 1,
    )

    payload = serialize_execution_snapshot(adapter.snapshot())
    restored_snapshot = deserialize_execution_snapshot(payload)
    restored = SimulatedExecutionAdapter.from_snapshot(restored_snapshot)

    assert restored.snapshot() == adapter.snapshot()
    assert restored.orders[0].status is OrderStatus.PARTIALLY_FILLED
    assert restored.reservations[0].remaining_amount == Decimal("600.6")

    completed, applied = restored.apply_fill(replace(
        restored.fills[0],
        fill_id="paper-fill-2",
        quantity=Decimal("6"),
        quote_notional=Decimal("600"),
        fee=Decimal("0.6"),
        executed_at=HOUR + 2,
        venue_fill_id="partial-2",
    ))
    assert applied
    assert completed.status is OrderStatus.FILLED
    assert restored.reservations[0].remaining_amount == 0


def test_cancelled_execution_snapshot_restores_terminal_reservation() -> None:
    adapter = _submitted_fake_adapter()
    cancelled = adapter.cancel(
        "paper-order",
        at=HOUR + 1,
        reason="deterministic cancellation",
    )
    assert cancelled.status is OrderStatus.CANCELLED

    restored = SimulatedExecutionAdapter.from_snapshot(
        deserialize_execution_snapshot(
            serialize_execution_snapshot(adapter.snapshot())
        )
    )

    assert restored.snapshot() == adapter.snapshot()
    assert restored.orders[0].status is OrderStatus.CANCELLED
    assert restored.reservations[0].remaining_amount == 0


def _submitted_fake_adapter() -> FakeVenueExecutionAdapter:
    instrument = Instrument(
        exchange="paper",
        market_type="spot",
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
    )
    intent = OrderIntent(
        intent_id="paper-intent",
        source_event_id="paper-source-event",
        source=IntentSource.STRATEGY,
        instrument=instrument,
        side=OrderSide.BUY,
        quantity=Decimal("10"),
        created_at=HOUR,
        earliest_execution_at=HOUR,
        signal_available_time=HOUR,
        source_candle_open_time=0,
        explanation="persistent execution snapshot test",
    )
    adapter = FakeVenueExecutionAdapter()
    adapter.submit(
        intent,
        order_id="paper-order",
        created_at=HOUR,
        reservation_amount=Decimal("1001"),
        available_balance=Decimal("1001"),
    )
    return adapter
