import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import ClassVar

import httpx
import pytest

from cryptolab.backtesting.accounting import PortfolioAccounting
from cryptolab.backtesting.models import PortfolioState
from cryptolab.backtesting.sizing import BuySizingProposal
from cryptolab.cli import main
from cryptolab.execution.models import (
    Instrument,
    IntentSource,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.bybit_client import BybitSpotClient
from cryptolab.market_data.models import Candle
from cryptolab.paper.execution import PaperExecutionSimulator
from cryptolab.paper.market import parse_bybit_last_price
from cryptolab.paper.models import (
    PaperBotConfig,
    PaperCycleResult,
    PaperExecutionConfig,
    PaperRiskConfig,
)
from cryptolab.paper.risk import PaperRiskManager
from cryptolab.paper.service import ContinuousPaperTrader
from cryptolab.replay.models import PriceObservation
from cryptolab.replay.risk import ReplayRiskContext
from cryptolab.storage.paper_state_repository import PaperStateRepository
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal


HOUR = 3_600_000


def candle(index: int, close: str = "100") -> Candle:
    price = Decimal(close)
    return Candle(
        open_time=index * HOUR,
        open=price,
        high=price + Decimal("2"),
        low=price - Decimal("2"),
        close=price,
        volume=Decimal("10"),
        close_time=(index + 1) * HOUR - 1,
        quote_volume=Decimal("1000"),
        number_of_trades=None,
        taker_buy_base_volume=None,
        taker_buy_quote_volume=None,
    )


def feature_row(index: int, price: str = "100") -> FeatureRow:
    value = Decimal(price)
    return FeatureRow(
        open_time=index * HOUR,
        close_time=(index + 1) * HOUR - 1,
        available_time=(index + 1) * HOUR,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1"),
        simple_return=None,
        log_return=None,
        sma20=None,
        sma50=None,
        ema20=None,
        rsi14=None,
        atr14=None,
        rvol20=None,
    )


@dataclass(frozen=True)
class ScriptedStrategy:
    signals: tuple[Signal, ...]
    name: ClassVar[str] = "stage3-scripted"

    @property
    def minimum_history(self) -> int:
        return 1

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: Sequence[FeatureRow],
    ):
        index = min(len(history) - 1, len(self.signals) - 1)
        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=history[-1],
            signal=self.signals[index],
            explanation="deterministic Stage 3 test signal",
        )


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class FakeMarketSource:
    def __init__(self, clock: Clock, candles: Sequence[Candle]) -> None:
        self.clock = clock
        self.candles = list(candles)
        self.price = Decimal("100")
        self.fail = False
        self.omit_last = False

    def load_closed_candles(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        *,
        received_at: int,
    ) -> list[Candle]:
        del symbol, interval, received_at
        if self.fail:
            raise httpx.ConnectError("offline")
        result = [
            item
            for item in self.candles
            if start_time <= item.open_time < end_time
        ]
        return result[:-1] if self.omit_last and result else result

    def observe_price(self, symbol: str) -> PriceObservation:
        del symbol
        if self.fail:
            raise httpx.ConnectError("offline")
        return PriceObservation(
            price=self.price,
            observed_at=self.clock.now,
            source="fake-public-ticker",
        )


def make_trader(
    tmp_path: Path,
    clock: Clock,
    source: FakeMarketSource,
    *,
    signals: tuple[Signal, ...] = (Signal.HOLD, Signal.BUY, Signal.SELL),
    warmup_candles: int = 2,
    execution_config: PaperExecutionConfig | None = None,
    sleep=None,
) -> ContinuousPaperTrader:
    return ContinuousPaperTrader(
        PaperBotConfig(
            session_id="paper-test",
            market_database=tmp_path / "market.db",
            state_database=tmp_path / "state.db",
            warmup_candles=warmup_candles,
            poll_seconds=1,
        ),
        ScriptedStrategy(signals),
        market_source=source,
        now_ms=clock,
        sleep=sleep or (lambda seconds: None),
        execution_config=execution_config,
    )


def test_initial_cycle_backfills_without_retroactive_orders_and_trades_latest(
    tmp_path: Path,
) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    trader = make_trader(tmp_path, clock, source)

    result = trader.run_once()
    state = trader.status()

    assert result.processed_candles == 2
    assert result.backfilled_candles == 1
    assert result.live_candles == 1
    assert result.fills_created == 1
    assert state.checkpoint_sequence == 2
    assert state.order_count == 1
    assert state.fill_count == 1
    assert state.base_balance > 0
    assert trader.repository.load_execution_snapshot(
        "paper-test"
    ).lifecycle.orders[0].status is OrderStatus.FILLED

    repeated = trader.run_once()
    assert repeated.processed_candles == 0
    assert trader.status() == state


def test_status_does_not_create_a_missing_session(tmp_path: Path) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    trader = make_trader(tmp_path, clock, source)

    with pytest.raises(RuntimeError, match="Unknown replay session"):
        trader.status()

    assert not trader.repository.session_exists("paper-test")


def test_continuous_loop_honors_graceful_stop(tmp_path: Path) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    stop = Event()

    def request_stop(seconds: float) -> None:
        assert seconds == 1
        stop.set()

    trader = make_trader(tmp_path, clock, source, sleep=request_stop)
    trader.run_forever(stop)

    assert stop.is_set()
    assert PaperStateRepository(
        tmp_path / "state.db"
    ).load_session("paper-test").checkpoint_sequence == 2


def test_continuous_loop_recovers_after_temporary_network_failure(
    tmp_path: Path,
) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    source.fail = True
    stop = Event()
    sleeps = 0

    def advance(seconds: float) -> None:
        nonlocal sleeps
        assert seconds == 1
        sleeps += 1
        if sleeps == 1:
            source.fail = False
        else:
            stop.set()

    trader = make_trader(tmp_path, clock, source, sleep=advance)
    trader.run_forever(stop)

    repository = PaperStateRepository(tmp_path / "state.db")
    assert sleeps == 2
    assert repository.load_session("paper-test").checkpoint_sequence == 2


def test_live_fill_uses_actual_observation_time_not_historical_next_open(
    tmp_path: Path,
) -> None:
    observation_time = 3 * HOUR + 12_345
    clock = Clock(observation_time)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    trader = make_trader(tmp_path, clock, source)

    trader.run_once()
    snapshot = trader.repository.load_execution_snapshot("paper-test")

    assert snapshot.lifecycle.fills[0].executed_at == observation_time
    assert snapshot.lifecycle.fills[0].executed_at > candle(2).close_time + 1


def test_observed_candle_duplicate_is_noop_and_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    trader = make_trader(tmp_path, clock, source)
    trader.run_once()
    session = trader.start()
    latest = candle(2)
    observation = PriceObservation(
        Decimal("100"),
        3 * HOUR,
        "fake-public-ticker",
    )

    repeated = session.process_observed_candle(
        latest,
        received_at=3 * HOUR,
        price_observation=observation,
        trading_enabled=True,
    )
    assert repeated.candle == latest
    assert trader.status().fill_count == 1

    with pytest.raises(RuntimeError, match="Conflicting duplicate"):
        session.process_observed_candle(
            latest,
            received_at=3 * HOUR,
            price_observation=PriceObservation(
                Decimal("101"),
                3 * HOUR,
                "fake-public-ticker",
            ),
            trading_enabled=True,
        )


def test_restart_recovers_then_sell_is_allowed_during_manual_pause(
    tmp_path: Path,
) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(
        clock,
        [candle(1), candle(2), candle(3, "110")],
    )
    first = make_trader(tmp_path, clock, source)
    first.run_once()
    first.repository.set_trading_paused("paper-test", True)
    first.close()

    clock.now = 4 * HOUR
    source.price = Decimal("110")
    recovered = make_trader(tmp_path, clock, source)
    result = recovered.run_once()
    status = recovered.status()

    assert result.fills_created == 1
    assert status.trading_paused is True
    assert status.base_balance == 0
    assert status.order_count == 2
    assert status.fill_count == 2


def test_incomplete_or_unavailable_market_data_never_advances_checkpoint(
    tmp_path: Path,
) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    source.omit_last = True
    trader = make_trader(tmp_path, clock, source)

    with pytest.raises(ValueError, match="expected 2"):
        trader.run_once()
    assert trader.status().checkpoint_sequence == 0

    source.omit_last = False
    source.fail = True
    with pytest.raises(httpx.ConnectError):
        trader.run_once()
    assert trader.status().checkpoint_sequence == 0


def test_backfill_actionable_signals_do_not_create_retroactive_orders(
    tmp_path: Path,
) -> None:
    clock = Clock(5 * HOUR)
    source = FakeMarketSource(
        clock,
        [candle(1), candle(2), candle(3), candle(4)],
    )
    trader = make_trader(
        tmp_path,
        clock,
        source,
        signals=(Signal.BUY, Signal.BUY, Signal.BUY, Signal.BUY),
        warmup_candles=4,
    )

    result = trader.run_once()

    assert result.backfilled_candles == 3
    assert result.live_candles == 1
    assert trader.status().order_count == 1
    assert trader.status().fill_count == 1


def test_runtime_partial_fill_is_cancelled_and_recovers_exactly(
    tmp_path: Path,
) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    execution = PaperExecutionConfig(
        partial_fill_fraction=Decimal("0.5"),
        cancel_unfilled_remainder=True,
    )
    trader = make_trader(
        tmp_path,
        clock,
        source,
        execution_config=execution,
    )
    trader.run_once()
    before = trader.status()
    trader.close()

    recovered = make_trader(
        tmp_path,
        clock,
        source,
        execution_config=execution,
    )
    after = recovered.check_integrity()

    assert after == before
    assert after.base_balance > 0
    assert after.base_balance < Decimal("9")
    assert after.reservation_count == 1
    snapshot = recovered.repository.load_execution_snapshot("paper-test")
    assert snapshot.lifecycle.orders[0].status is OrderStatus.CANCELLED
    assert snapshot.lifecycle.reservations[0].remaining_amount == 0


def test_restart_preserves_pending_partial_order_and_reservation(
    tmp_path: Path,
) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    execution = PaperExecutionConfig(
        partial_fill_fraction=Decimal("0.5"),
        cancel_unfilled_remainder=False,
    )
    trader = make_trader(
        tmp_path,
        clock,
        source,
        execution_config=execution,
    )
    trader.run_once()
    trader.close()

    recovered = make_trader(
        tmp_path,
        clock,
        source,
        execution_config=execution,
    )
    status = recovered.check_integrity()
    snapshot = recovered.repository.load_execution_snapshot("paper-test")

    assert status.reservation_count == 1
    assert snapshot.lifecycle.orders[0].status is OrderStatus.PARTIALLY_FILLED
    assert snapshot.lifecycle.reservations[0].remaining_amount > 0


def test_manual_pause_rejects_buy_without_stopping_candle_processing(
    tmp_path: Path,
) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    trader = make_trader(tmp_path, clock, source)
    trader.start()
    trader.repository.set_trading_paused("paper-test", True)

    result = trader.run_once()
    status = trader.status()

    assert result.processed_candles == 2
    assert result.fills_created == 0
    assert status.trading_paused is True
    assert status.base_balance == 0
    assert status.order_count == 0


def test_paper_risk_pause_daily_loss_drawdown_and_sell_policy() -> None:
    config = PaperRiskConfig(
        maximum_daily_loss=Decimal("0.05"),
        maximum_portfolio_drawdown=Decimal("0.10"),
    )
    manager = PaperRiskManager(config)
    instrument = Instrument("bybit", "spot", "BTCUSDT", "BTC", "USDT")
    portfolio = PortfolioState(
        quote_balance=Decimal("1000"),
        base_balance=Decimal("0"),
        entry_cost=Decimal("0"),
        realized_pnl=Decimal("0"),
        fees_paid=Decimal("0"),
    )

    def context(signal: Signal, equity: Decimal, at: int):
        decision = make_decision(
            strategy_name="risk-test",
            symbol="BTCUSDT",
            interval="1h",
            row=feature_row(0),
            signal=signal,
            explanation="risk test",
        )
        return ReplayRiskContext(
            decision=decision,
            sizing=(
                None
                if signal is not Signal.BUY
                else BuySizingProposal(Decimal("100"), "test")
            ),
            portfolio=portfolio,
            instrument=instrument,
            market_open_price=Decimal("100"),
            observed_at=at,
            candle_close_time=at - 1,
            equity=equity,
        )

    assert manager.check(context(Signal.HOLD, Decimal("1000"), HOUR)).accepted
    manager.set_paused(True)
    assert not manager.check(context(Signal.BUY, Decimal("1000"), HOUR)).accepted
    assert manager.check(context(Signal.SELL, Decimal("1000"), HOUR)).accepted
    manager.set_paused(False)
    daily = manager.check(context(Signal.BUY, Decimal("940"), 2 * HOUR))
    assert not daily.accepted
    assert "daily loss" in daily.reason

    drawdown_manager = PaperRiskManager(config)
    assert drawdown_manager.check(
        context(Signal.HOLD, Decimal("1200"), HOUR)
    ).accepted
    drawdown = drawdown_manager.check(
        context(Signal.BUY, Decimal("1000"), 25 * HOUR)
    )
    assert not drawdown.accepted
    assert "drawdown" in drawdown.reason

    stale_manager = PaperRiskManager(config)
    stale = stale_manager.check(replace(
        context(
            Signal.BUY,
            Decimal("1000"),
            HOUR + config.data_freshness_limit_ms + 1,
        ),
        candle_close_time=HOUR - 1,
    ))
    assert not stale.accepted
    assert "freshness" in stale.reason


def test_partial_fill_and_cancellation_use_shared_lifecycle_and_accounting() -> None:
    instrument = Instrument("bybit", "spot", "BTCUSDT", "BTC", "USDT")
    intent = OrderIntent(
        intent_id="intent-1",
        source_event_id="decision-1",
        source=IntentSource.STRATEGY,
        instrument=instrument,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        created_at=HOUR,
        earliest_execution_at=HOUR,
        signal_available_time=HOUR,
        source_candle_open_time=0,
        explanation="paper partial fill",
    )
    simulator = PaperExecutionSimulator(PaperExecutionConfig(
        quantity_step=Decimal("0.01"),
        minimum_quantity=Decimal("0.01"),
        minimum_notional=Decimal("1"),
        simulated_latency_ms=250,
        partial_fill_fraction=Decimal("0.5"),
        cancel_unfilled_remainder=True,
    ))

    result = simulator.submit_and_simulate(
        intent,
        observed_price=Decimal("100"),
        observed_at=HOUR,
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.0005"),
        available_balance=Decimal("1000"),
    )
    accounting = PortfolioAccounting(Decimal("1000"))
    simulator.apply_fill_to_accounting(accounting, result.fill)

    assert result.fill.quantity == Decimal("0.5")
    assert result.fill.executed_at == HOUR + 250
    assert result.order.status is OrderStatus.CANCELLED
    assert result.remainder_cancelled is True
    assert simulator.adapter.reservations[0].remaining_amount == 0
    assert accounting.base_balance(instrument) == Decimal("0.5")
    assert accounting.quote_balance >= 0
    accounting.assert_invariants()

    repeated = simulator.submit_and_simulate(
        intent,
        observed_price=Decimal("100"),
        observed_at=HOUR,
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.0005"),
        available_balance=Decimal("1000"),
    )
    assert repeated.fill == result.fill
    assert repeated.fill_applied is False
    assert repeated.order.status is OrderStatus.CANCELLED


def test_fake_venue_rejection_releases_reservation_idempotently() -> None:
    instrument = Instrument("bybit", "spot", "BTCUSDT", "BTC", "USDT")
    intent = OrderIntent(
        intent_id="rejected-intent",
        source_event_id="rejected-decision",
        source=IntentSource.STRATEGY,
        instrument=instrument,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        created_at=HOUR,
        earliest_execution_at=HOUR,
        signal_available_time=HOUR,
        source_candle_open_time=0,
        explanation="fake rejection",
    )
    simulator = PaperExecutionSimulator()

    order = simulator.submit_rejected(
        intent,
        reservation_amount=Decimal("100"),
        available_balance=Decimal("1000"),
        rejected_at=HOUR,
        reason="deterministic fake venue rejection",
    )
    repeated = simulator.submit_rejected(
        intent,
        reservation_amount=Decimal("100"),
        available_balance=Decimal("1000"),
        rejected_at=HOUR,
        reason="deterministic fake venue rejection",
    )

    assert order == repeated
    assert order.status is OrderStatus.REJECTED
    assert simulator.adapter.reservations[0].remaining_amount == 0


def test_bybit_ticker_client_and_parser_use_public_spot_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v5/market/tickers"
        assert request.url.params["category"] == "spot"
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(200, json={
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "spot",
                "list": [{"symbol": "BTCUSDT", "lastPrice": "61234.5"}],
            },
        })

    raw = BybitSpotClient(
        transport=httpx.MockTransport(handler),
    ).get_ticker("BTCUSDT")
    assert parse_bybit_last_price(raw, "BTCUSDT") == Decimal("61234.5")

    with pytest.raises(ValueError, match="exactly one"):
        parse_bybit_last_price({
            "retCode": 0,
            "result": {"category": "spot", "list": []},
        }, "BTCUSDT")


def test_schema_v1_migrates_controls_without_losing_session(tmp_path: Path) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    trader = make_trader(tmp_path, clock, source)
    trader.run_once()
    trader.close()
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE paper_controls")
        connection.execute("UPDATE paper_sessions SET schema_version = 1")
        connection.execute("PRAGMA user_version = 1")

    repository = PaperStateRepository(database)
    repository.init_schema()

    assert repository.session_exists("paper-test")
    assert repository.get_trading_paused("paper-test") is False
    assert repository.load_session("paper-test").checkpoint_sequence == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_integrity_rejects_corrupt_manual_control_state(tmp_path: Path) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    trader = make_trader(tmp_path, clock, source)
    trader.start()
    trader.close()
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute(
            "UPDATE paper_controls SET revision = -1 "
            "WHERE session_id = 'paper-test'"
        )

    recovered = make_trader(tmp_path, clock, source)
    with pytest.raises(RuntimeError, match="control revision"):
        recovered.check_integrity()


def test_paper_cli_run_once_uses_stage3_dispatch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    class FakeTrader:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def run_once(self) -> PaperCycleResult:
            return PaperCycleResult(
                session_id="cli-paper",
                checked_at=HOUR,
                range_start=None,
                range_end=None,
                processed_candles=0,
                backfilled_candles=0,
                live_candles=0,
                fills_created=0,
                trading_paused=False,
                message="no newly closed candle is available",
            )

        def close(self) -> None:
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cryptolab.cli.ContinuousPaperTrader", FakeTrader)

    result = main(["paper", "run-once", "--session-id", "cli-paper"])

    assert result == 0
    output = capsys.readouterr().out
    assert "session=cli-paper" in output
    assert "simulated" not in output.lower()


def test_paper_cli_pause_and_resume_are_durable(
    tmp_path: Path,
    capsys,
) -> None:
    clock = Clock(3 * HOUR)
    source = FakeMarketSource(clock, [candle(1), candle(2)])
    trader = make_trader(tmp_path, clock, source)
    trader.start()
    state_database = tmp_path / "state.db"

    assert main([
        "paper",
        "pause",
        "--session-id",
        "paper-test",
        "--state-database",
        str(state_database),
    ]) == 0
    assert trader.repository.get_trading_paused("paper-test") is True

    assert main([
        "paper",
        "resume",
        "--session-id",
        "paper-test",
        "--state-database",
        str(state_database),
    ]) == 0
    assert trader.repository.get_trading_paused("paper-test") is False
    assert "resumed" in capsys.readouterr().out
