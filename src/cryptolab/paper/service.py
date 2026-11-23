"""Long-running, restart-safe Stage 3 paper-trading orchestration."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event

from cryptolab.backtesting.models import BacktestConfig
from cryptolab.backtesting.sizing import LegacyAllInAllOutSizer, PositionSizer
from cryptolab.market_data.intervals import get_interval_milliseconds
from cryptolab.market_data.validator import validate_historical_candles
from cryptolab.paper.execution import PaperPositionSizer
from cryptolab.paper.market import BybitPaperMarketSource, PaperMarketSource
from cryptolab.paper.models import (
    PaperBotConfig,
    PaperCycleResult,
    PaperExecutionConfig,
    PaperRiskConfig,
    PaperSessionStatus,
)
from cryptolab.paper.observability import PaperTelemetry
from cryptolab.paper.risk import PaperRiskManager
from cryptolab.replay.models import PriceObservation, ReplayExecutionMode
from cryptolab.replay.persistence_models import ReplayStateIntegrityError
from cryptolab.replay.persistent import (
    PersistentReplayRunner,
    PersistentReplaySession,
)
from cryptolab.storage.paper_state_repository import PaperStateRepository
from cryptolab.strategies.base import Strategy


LOGGER = logging.getLogger(__name__)


class ContinuousPaperTrader:
    """Coordinate market recovery, observed execution, risk, and persistence."""

    def __init__(
        self,
        config: PaperBotConfig,
        strategy: Strategy,
        *,
        backtest_config: BacktestConfig | None = None,
        risk_config: PaperRiskConfig | None = None,
        execution_config: PaperExecutionConfig | None = None,
        position_sizer: PositionSizer | None = None,
        market_source: PaperMarketSource | None = None,
        now_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] | None = None,
        telemetry: PaperTelemetry | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.backtest_config = backtest_config or BacktestConfig()
        self.risk_manager = PaperRiskManager(risk_config)
        self.execution_config = execution_config or PaperExecutionConfig()
        delegate = position_sizer or LegacyAllInAllOutSizer()
        self.position_sizer = PaperPositionSizer(
            delegate=delegate,
            execution=self.execution_config,
        )
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._sleep = sleep
        self.telemetry = telemetry
        config.market_database.parent.mkdir(parents=True, exist_ok=True)
        config.state_database.parent.mkdir(parents=True, exist_ok=True)
        self.market_source = market_source or BybitPaperMarketSource(
            config.market_database,
            now_ms=self._now_ms,
        )
        self._runner = PersistentReplayRunner(
            config.state_database,
            self.backtest_config,
            position_sizer=self.position_sizer,
            risk_policy=self.risk_manager,
            execution_mode=ReplayExecutionMode.OBSERVED_AFTER_CLOSE,
            execution_fill_fraction=(
                self.execution_config.partial_fill_fraction
            ),
            execution_quantity_step=self.execution_config.quantity_step,
            cancel_unfilled_remainder=(
                self.execution_config.cancel_unfilled_remainder
            ),
        )
        self._session: PersistentReplaySession | None = None

    @property
    def repository(self) -> PaperStateRepository:
        return self._runner.repository

    def start(
        self,
        *,
        create_if_missing: bool = True,
    ) -> PersistentReplaySession:
        """Create or deterministically recover the configured paper session."""
        if self._session is not None:
            return self._session
        recovered = self.repository.session_exists(self.config.session_id)
        if recovered:
            session = self._runner.recover(
                self.config.session_id,
                self.strategy,
                self.config.symbol,
                self.config.interval,
                exchange=self.config.exchange,
                market_type=self.config.market_type,
            )
        elif create_if_missing:
            session = self._runner.create(
                self.config.session_id,
                self.strategy,
                self.config.symbol,
                self.config.interval,
                exchange=self.config.exchange,
                market_type=self.config.market_type,
            )
        else:
            raise ReplayStateIntegrityError(
                f"Unknown replay session: {self.config.session_id}"
            )
        self.risk_manager.set_paused(
            self.repository.get_trading_paused(self.config.session_id)
        )
        self._session = session
        if self.telemetry is not None:
            execution = self.repository.load_execution_snapshot(
                self.config.session_id
            )
            self.telemetry.started(
                recovered=recovered,
                orders=execution.lifecycle.orders,
                fills=execution.lifecycle.fills,
                checkpoint_sequence=session.current_state.checkpoint_sequence,
            )
        return session

    def run_once(self) -> PaperCycleResult:
        """Recover every missing closed candle and trade only the newest one."""
        session = self.start()
        self.risk_manager.set_paused(
            self.repository.get_trading_paused(self.config.session_id)
        )
        checked_at = self._now_ms()
        interval_ms = get_interval_milliseconds(self.config.interval)
        closed_range_end = checked_at - checked_at % interval_ms
        state = session.current_state
        if state.checkpoint_open_time is None:
            range_start = max(
                0,
                closed_range_end - self.config.warmup_candles * interval_ms,
            )
        else:
            range_start = state.checkpoint_open_time + interval_ms
        requested = max(0, (closed_range_end - range_start) // interval_ms)
        if state.checkpoint_sequence + requested > self.config.maximum_session_candles:
            raise ReplayStateIntegrityError(
                "Paper session candle limit would be exceeded; create a new "
                "session after archiving and verifying the current database"
            )
        if range_start >= closed_range_end:
            result = PaperCycleResult(
                session_id=self.config.session_id,
                checked_at=checked_at,
                range_start=None,
                range_end=None,
                processed_candles=0,
                backfilled_candles=0,
                live_candles=0,
                fills_created=0,
                trading_paused=self.risk_manager.paused,
                message="no newly closed candle is available",
            )
            self._observe_telemetry(session, result)
            return result

        candles = self.market_source.load_closed_candles(
            self.config.symbol,
            self.config.interval,
            range_start,
            closed_range_end,
            received_at=checked_at,
        )
        self._validate_exact_range(
            candles,
            range_start,
            closed_range_end,
            checked_at,
        )
        received_at = max(checked_at, self._now_ms())
        processed = 0
        backfilled = 0
        live = 0
        fills_before = state.fill_count
        for index, candle in enumerate(candles):
            newest = index == len(candles) - 1
            data_age = received_at - (candle.close_time + 1)
            trading_enabled = (
                newest
                and data_age <= self.risk_manager.config.data_freshness_limit_ms
            )
            observation: PriceObservation | None = None
            if trading_enabled:
                observation = self.market_source.observe_price(
                    self.config.symbol
                )
                if observation.observed_at < received_at:
                    raise ValueError(
                        "Observed public price predates candle receipt"
                    )
                if self.execution_config.simulated_latency_ms:
                    delay = self.execution_config.simulated_latency_ms / 1000
                    if self._sleep is None:
                        time.sleep(delay)
                    else:
                        self._sleep(delay)
                    observation = PriceObservation(
                        price=observation.price,
                        observed_at=max(
                            self._now_ms(),
                            observation.observed_at
                            + self.execution_config.simulated_latency_ms,
                        ),
                        source=(
                            observation.source
                            + "+configured_simulated_latency"
                        ),
                    )
            session.process_observed_candle(
                candle,
                received_at=received_at,
                price_observation=observation,
                trading_enabled=trading_enabled,
            )
            processed += 1
            if trading_enabled:
                live += 1
            else:
                backfilled += 1
        current = session.current_state
        result = PaperCycleResult(
            session_id=self.config.session_id,
            checked_at=checked_at,
            range_start=range_start,
            range_end=closed_range_end,
            processed_candles=processed,
            backfilled_candles=backfilled,
            live_candles=live,
            fills_created=current.fill_count - fills_before,
            trading_paused=self.risk_manager.paused,
            message="paper cycle completed",
        )
        self._observe_telemetry(session, result)
        return result

    def run_forever(self, stop_event: Event | None = None) -> None:
        """Poll until graceful stop; integrity failures remain fail-closed."""
        stop = stop_event or Event()
        self.start()
        try:
            while not stop.is_set():
                try:
                    result = self.run_once()
                    LOGGER.info(
                        "Paper cycle: processed=%s backfill=%s live=%s fills=%s",
                        result.processed_candles,
                        result.backfilled_candles,
                        result.live_candles,
                        result.fills_created,
                    )
                except ReplayStateIntegrityError:
                    if self.telemetry is not None:
                        self.telemetry.event(
                            "fail_closed",
                            "Paper state integrity failed; stopping fail-closed",
                            dedup_key=(
                                f"fail-closed:"
                                f"{self.config.session_id}:"
                                f"{self._now_ms()}"
                            ),
                            notify=True,
                            level=logging.ERROR,
                        )
                    LOGGER.exception(
                        "Paper state integrity failed; stopping fail-closed"
                    )
                    raise
                except Exception:
                    if self.telemetry is not None:
                        self.telemetry.event(
                            "paper_cycle_error",
                            "Paper cycle failed at the durable checkpoint",
                            level=logging.ERROR,
                        )
                    LOGGER.exception(
                        "Paper cycle failed; state remains at last checkpoint"
                    )
                    # The persistent handle may have mutated in memory before
                    # a failed commit. Discard it and reconstruct exclusively
                    # from the last durable checkpoint on the next cycle.
                    self.close()
                if not stop.is_set():
                    self._wait_for_next_cycle(stop)
        finally:
            if self.telemetry is not None and self._session is not None:
                self.telemetry.stopped(
                    self._session.current_state.checkpoint_sequence
                )
            self.close()

    def _wait_for_next_cycle(self, stop: Event) -> None:
        """Wait for polling while allowing SIGTERM to wake the process."""
        if self._sleep is None:
            stop.wait(self.config.poll_seconds)
        else:
            # Kept for deterministic unit clocks. Production always follows
            # the interruptible Event.wait path above.
            self._sleep(self.config.poll_seconds)

    def status(self) -> PaperSessionStatus:
        session = self.start(create_if_missing=False)
        state = session.current_state
        paused = self.repository.get_trading_paused(self.config.session_id)
        return PaperSessionStatus(
            session_id=self.config.session_id,
            checkpoint_sequence=state.checkpoint_sequence,
            checkpoint_open_time=state.checkpoint_open_time,
            quote_balance=state.quote_balance,
            base_balance=state.base_balance,
            realized_pnl=state.realized_pnl,
            fees_paid=state.fees_paid,
            order_count=state.order_count,
            fill_count=state.fill_count,
            reservation_count=state.reservation_count,
            trading_paused=paused,
        )

    def check_integrity(self) -> PaperSessionStatus:
        """Recovery itself performs full checkpoint and lifecycle comparison."""
        return self.status()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _observe_telemetry(
        self,
        session: PersistentReplaySession,
        result: PaperCycleResult,
    ) -> None:
        if self.telemetry is None:
            return
        checkpoint = session.operational_checkpoint
        execution = checkpoint.execution
        correlation_id = (
            f"paper-cycle:{self.config.session_id}:{result.checked_at}"
        )
        self.telemetry.execution(
            execution.orders,
            execution.execution_fills,
            correlation_id=correlation_id,
        )
        equity = (
            checkpoint.snapshots[-1].equity
            if checkpoint.snapshots
            else self.backtest_config.initial_cash
        )
        risk_state = self.risk_manager.snapshot_state()
        self.telemetry.portfolio(
            cash=execution.state.quote_balance,
            equity=equity,
            paused=self.risk_manager.paused,
            last_candle_open_time=(
                checkpoint.candles[-1].open_time
                if checkpoint.candles
                else None
            ),
            decision_available_time=(
                checkpoint.decisions[-1].signal_available_time
                if checkpoint.decisions
                else None
            ),
            interval_ms=get_interval_milliseconds(self.config.interval),
            risk_rejections=int(risk_state["rejections"]),
            reconciled=True,
            correlation_id=correlation_id,
            peak_equity=risk_state["peak_equity"],
        )
        if result.processed_candles and result.live_candles == 0:
            self.telemetry.stale(
                candle_open_time=checkpoint.candles[-1].open_time,
                correlation_id=correlation_id,
            )
        self.telemetry.flush()

    def _validate_exact_range(
        self,
        candles,
        start_time: int,
        end_time: int,
        received_at: int,
    ) -> None:
        interval_ms = get_interval_milliseconds(self.config.interval)
        expected = (end_time - start_time) // interval_ms
        if len(candles) != expected:
            raise ValueError(
                f"Paper source returned {len(candles)} candles; "
                f"expected {expected}"
            )
        if not candles or candles[0].open_time != start_time:
            raise ValueError("Paper source did not start at requested boundary")
        if candles[-1].open_time != end_time - interval_ms:
            raise ValueError("Paper source did not reach requested boundary")
        validate_historical_candles(
            candles,
            self.config.interval,
            as_of_time=received_at,
        )


def set_paper_trading_pause(
    database_path: str | Path,
    session_id: str,
    paused: bool,
) -> int:
    repository = PaperStateRepository(database_path)
    repository.init_schema()
    return repository.set_trading_paused(session_id, paused)
