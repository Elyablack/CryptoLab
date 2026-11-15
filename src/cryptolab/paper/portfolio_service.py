"""Restart-safe BTC/ETH shared-cash paper portfolio orchestration."""

from __future__ import annotations

import logging
import time
from hashlib import sha256
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from threading import Event

from cryptolab.backtesting.accounting import PortfolioAccounting
from cryptolab.backtesting.models import BacktestConfig, PortfolioState
from cryptolab.backtesting.sizing import (
    BuySizingProposal,
    LegacyAllInAllOutSizer,
    PositionSizer,
    SellSizingProposal,
    SizingContext,
    SizingRejection,
)
from cryptolab.execution.adapter import SimulatedExecutionAdapter
from cryptolab.execution.models import (
    Instrument,
    IntentSource,
    OrderIntent,
    OrderSide,
)
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.intervals import get_interval_milliseconds
from cryptolab.market_data.models import Candle
from cryptolab.market_data.validator import validate_historical_candles
from cryptolab.paper.execution import (
    PaperExecutionSimulator,
    PaperPositionSizer,
    floor_to_step,
)
from cryptolab.paper.market import BybitPaperMarketSource, PaperMarketSource
from cryptolab.paper.models import (
    MultiSymbolPaperBotConfig,
    MultiSymbolPaperCycleResult,
    MultiSymbolPaperSessionStatus,
    PaperExecutionConfig,
    PaperRiskConfig,
    PendingRotation,
    PortfolioReconciliationReport,
    RotationPhase,
)
from cryptolab.paper.observability import PaperTelemetry
from cryptolab.paper.risk import PaperRiskManager
from cryptolab.replay.models import PriceObservation
from cryptolab.replay.persistence_models import ReplayStateIntegrityError
from cryptolab.replay.risk import ReplayRiskContext
from cryptolab.replay.serialization import (
    canonical_json,
    decode_mapping,
    deserialize_candle,
    deserialize_execution_snapshot,
    serialize_candle,
    serialize_execution_snapshot,
)
from cryptolab.storage.paper_state_repository import PaperStateRepository
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal
from cryptolab.strategies.relative_momentum import (
    RelativeMomentumRotationPlanner,
    RotationTarget,
)


LOGGER = logging.getLogger(__name__)
ZERO = Decimal("0")
ONE = Decimal("1")


class MultiSymbolPaperTrader:
    """Run fixed BTC/ETH Rotation through shared accounting and lifecycle.

    This is an orchestration service, not another execution engine. Confirmed
    fills are produced by ``PaperExecutionSimulator`` and settled exclusively
    by ``PortfolioAccounting``.
    """

    def __init__(
        self,
        config: MultiSymbolPaperBotConfig,
        *,
        backtest_config: BacktestConfig | None = None,
        risk_config: PaperRiskConfig | None = None,
        execution_config: PaperExecutionConfig | None = None,
        position_sizer: PositionSizer | None = None,
        market_source: PaperMarketSource | None = None,
        now_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] | None = None,
        after_sell_checkpoint: Callable[[], None] | None = None,
        rotation_planner: RelativeMomentumRotationPlanner | None = None,
        telemetry: PaperTelemetry | None = None,
    ) -> None:
        self.config = config
        self.backtest_config = backtest_config or BacktestConfig()
        self.execution_config = execution_config or PaperExecutionConfig()
        self.risk_manager = PaperRiskManager(risk_config)
        delegate = position_sizer or LegacyAllInAllOutSizer()
        self.position_sizer = PaperPositionSizer(
            delegate=delegate,
            execution=self.execution_config,
        )
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._sleep = sleep or time.sleep
        self._after_sell_checkpoint = after_sell_checkpoint
        self.telemetry = telemetry
        config.market_database.parent.mkdir(parents=True, exist_ok=True)
        config.state_database.parent.mkdir(parents=True, exist_ok=True)
        self.market_source = market_source or BybitPaperMarketSource(
            config.market_database,
            now_ms=self._now_ms,
        )
        self.repository = PaperStateRepository(config.state_database)
        self.repository.init_schema()
        self._planner = rotation_planner or RelativeMomentumRotationPlanner()
        if config.warmup_candles <= self._planner.lookback_candles:
            raise ValueError(
                "warmup_candles must exceed the momentum lookback"
            )
        self._builder = FeatureBuilder()
        self._instruments = {
            symbol: Instrument(
                exchange=config.exchange,
                market_type=config.market_type,
                symbol=symbol,
                base_asset=symbol.removesuffix("USDT"),
                quote_asset="USDT",
            )
            for symbol in config.symbols
        }
        self._started = False
        self._candles: dict[str, list[Candle]] = {
            symbol: [] for symbol in config.symbols
        }
        self._rows: dict[str, list[FeatureRow]] = {
            symbol: [] for symbol in config.symbols
        }
        self._latest_prices: dict[str, Decimal] = {}
        self._accounting = PortfolioAccounting(
            self.backtest_config.initial_cash
        )
        self._adapter = SimulatedExecutionAdapter()
        self._simulator = PaperExecutionSimulator(
            self.execution_config,
            adapter=self._adapter,
        )
        self._pending: PendingRotation | None = None
        self._rotation_target = RotationTarget.CASH.value
        self._sequence = 0
        self._revision = 0
        self._reconciled = True
        self._diagnostics: tuple[str, ...] = ()
        self._integrity_faults: tuple[str, ...] = ()

    def start(self, *, create_if_missing: bool = True) -> None:
        """Create or restore a portfolio session and reconcile immediately."""
        if self._started:
            return
        recovered = self.repository.portfolio_session_exists(
            self.config.session_id
        )
        if recovered:
            self._reset_runtime_state()
            self._restore()
        elif create_if_missing:
            self._create()
        else:
            raise ReplayStateIntegrityError(
                f"Unknown portfolio paper session: {self.config.session_id}"
            )
        self.risk_manager.set_paused(
            self.repository.get_portfolio_trading_paused(
                self.config.session_id
            )
        )
        self._started = True
        report = self.reconcile(persist=True)
        if self.telemetry is not None:
            self.telemetry.started(
                recovered=recovered,
                orders=self._adapter.orders,
                fills=self._adapter.fills,
                checkpoint_sequence=self._sequence,
            )
            self._observe_telemetry(report, self._now_ms())

    def run_once(self) -> MultiSymbolPaperCycleResult:
        """Run one cycle and discard non-durable mutations after failures."""
        try:
            return self._run_once_impl()
        except Exception:
            if self.telemetry is not None:
                self.telemetry.event(
                    "paper_cycle_error",
                    "Portfolio paper cycle failed at durable state",
                    level=logging.ERROR,
                )
            self._reset_runtime_state()
            self._started = False
            raise

    def _run_once_impl(self) -> MultiSymbolPaperCycleResult:
        """Process only complete synchronized BTC/ETH candle sets."""
        self.start()
        self.risk_manager.set_paused(
            self.repository.get_portfolio_trading_paused(
                self.config.session_id
            )
        )
        checked_at = self._now_ms()
        interval_ms = get_interval_milliseconds(self.config.interval)
        closed_range_end = checked_at - checked_at % interval_ms
        range_start = (
            max(
                0,
                closed_range_end
                - self.config.warmup_candles * interval_ms,
            )
            if self._sequence == 0
            else self._last_open_time() + interval_ms
        )
        fills_before = len(self._adapter.fills)
        if range_start >= closed_range_end:
            live = self._resume_pending(checked_at)
            report = self.reconcile(persist=True)
            result = self._cycle_result(
                checked_at=checked_at,
                range_start=None,
                range_end=None,
                processed=0,
                backfilled=0,
                live=live,
                fills_before=fills_before,
                report=report,
                message=(
                    "pending rotation evaluated"
                    if live
                    else "no newly closed synchronized candle set is available"
                ),
            )
            self._observe_telemetry(report, checked_at, result=result)
            return result

        loaded = {
            symbol: self.market_source.load_closed_candles(
                symbol,
                self.config.interval,
                range_start,
                closed_range_end,
                received_at=checked_at,
            )
            for symbol in self.config.symbols
        }
        self._validate_synchronized_range(
            loaded,
            range_start,
            closed_range_end,
            checked_at,
        )
        processed = 0
        backfilled = 0
        live = 0
        received_at = max(checked_at, self._now_ms())
        pair_count = len(loaded[self.config.symbols[0]])
        for index in range(pair_count):
            pair = {
                symbol: loaded[symbol][index]
                for symbol in self.config.symbols
            }
            newest = index == pair_count - 1
            candle = pair[self.config.symbols[0]]
            age = received_at - (candle.close_time + 1)
            trading_enabled = (
                newest
                and age <= self.risk_manager.config.data_freshness_limit_ms
            )
            self._process_pair(
                pair,
                received_at=received_at,
                trading_enabled=trading_enabled,
            )
            processed += 1
            if trading_enabled:
                live += 1
            else:
                backfilled += 1
        report = self.reconcile(persist=True)
        result = self._cycle_result(
            checked_at=checked_at,
            range_start=range_start,
            range_end=closed_range_end,
            processed=processed,
            backfilled=backfilled,
            live=live,
            fills_before=fills_before,
            report=report,
            message="multi-symbol paper cycle completed",
        )
        self._observe_telemetry(report, checked_at, result=result)
        return result

    def run_forever(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        self.start()
        try:
            while not stop.is_set():
                try:
                    result = self.run_once()
                    LOGGER.info(
                        "Portfolio paper cycle: sets=%s live=%s fills=%s "
                        "reconciled=%s",
                        result.processed_candle_sets,
                        result.live_candle_sets,
                        result.fills_created,
                        result.reconciled,
                    )
                except ReplayStateIntegrityError:
                    if self.telemetry is not None:
                        self.telemetry.event(
                            "fail_closed",
                            "Portfolio integrity failed; stopping fail-closed",
                            dedup_key=f"fail-closed:{self._sequence}",
                            notify=True,
                            level=logging.ERROR,
                        )
                    LOGGER.exception(
                        "Portfolio paper integrity failed; stopping fail-closed"
                    )
                    raise
                except Exception:
                    LOGGER.exception(
                        "Portfolio paper cycle failed at last durable checkpoint"
                    )
                    self._reset_runtime_state()
                    self._started = False
                if not stop.is_set():
                    self._sleep(self.config.poll_seconds)
        finally:
            if self.telemetry is not None:
                self.telemetry.stopped(self._sequence)

    def status(self) -> MultiSymbolPaperSessionStatus:
        self.start(create_if_missing=False)
        report = self.reconcile(persist=True)
        return self._status(report)

    def check_integrity(self) -> MultiSymbolPaperSessionStatus:
        status = self.status()
        if not status.reconciled:
            raise ReplayStateIntegrityError(
                "Portfolio reconciliation failed: "
                + "; ".join(status.reconciliation_diagnostics)
            )
        return status

    def reconcile(
        self,
        *,
        persist: bool,
    ) -> PortfolioReconciliationReport:
        """Rebuild FIFO from fills and compare independent state domains.

        The independent financial comparison is the execution fill journal
        versus a newly rebuilt ``PortfolioAccounting`` instance. Adapter
        snapshot round-tripping is only an integrity check and is deliberately
        not described as independent venue reconciliation.
        """
        report = self._build_reconciliation(self._now_ms())
        self._reconciled = report.consistent
        self._diagnostics = report.diagnostics
        if persist:
            self._commit_state(report=report)
        return report

    def _create(self) -> None:
        report = self._build_reconciliation(self._now_ms())
        self.repository.create_portfolio_session(
            session_id=self.config.session_id,
            configuration_json=self._configuration_json(),
            checkpoint_json=self._checkpoint_json(),
            execution_json=serialize_execution_snapshot(
                self._adapter.snapshot()
            ),
            risk_state_json=canonical_json(
                self.risk_manager.snapshot_state()
            ),
            diagnostics_json=canonical_json({"diagnostics": []}),
        )
        self._reconciled = report.consistent

    def _restore(self) -> None:
        stored = self.repository.load_portfolio_session(
            self.config.session_id
        )
        if stored.configuration_json != self._configuration_json():
            raise ReplayStateIntegrityError(
                "Portfolio paper configuration differs from stored session"
            )
        self._sequence = stored.candle_sequence
        self._revision = stored.revision
        for _, symbol, _, payload in stored.candles:
            self._candles[symbol].append(deserialize_candle(payload))
        self._rebuild_features()
        self._adapter = SimulatedExecutionAdapter.from_snapshot(
            deserialize_execution_snapshot(stored.execution_json)
        )
        self._simulator = PaperExecutionSimulator(
            self.execution_config,
            adapter=self._adapter,
        )
        self._accounting = self._accounting_from_fills(self._adapter.fills)
        self.risk_manager.restore_state(
            decode_mapping(stored.risk_state_json)
        )
        self._reconciled = stored.reconciled
        diagnostics = decode_mapping(stored.diagnostics_json).get(
            "diagnostics", []
        )
        if not isinstance(diagnostics, list) or not all(
            isinstance(item, str) for item in diagnostics
        ):
            raise ReplayStateIntegrityError(
                "Invalid portfolio reconciliation diagnostics"
            )
        self._diagnostics = tuple(diagnostics)
        if not stored.reconciled:
            # Reconciliation failures are a durable fail-closed latch. They
            # must not disappear merely because the same corrupted snapshot
            # is internally self-consistent after another restart.
            self._integrity_faults = tuple(diagnostics) or (
                "stored portfolio checkpoint is marked unreconciled",
            )
        checkpoint = decode_mapping(stored.checkpoint_json)
        self._restore_checkpoint(checkpoint)

    def _process_pair(
        self,
        pair: Mapping[str, Candle],
        *,
        received_at: int,
        trading_enabled: bool,
    ) -> None:
        for symbol in self.config.symbols:
            self._candles[symbol].append(pair[symbol])
            self._latest_prices[symbol] = pair[symbol].close
        self._rebuild_features()
        candle_payloads = tuple(
            (
                symbol,
                pair[symbol].open_time,
                serialize_candle(pair[symbol]),
            )
            for symbol in self.config.symbols
        )
        if not trading_enabled:
            self._observe_hold_risk(pair, received_at)
            self._commit_state(candles=candle_payloads)
            return

        review = self._review_for_latest_pair()
        if self._pending is None and review is not None:
            if self.telemetry is not None:
                self.telemetry.event(
                    "strategy_decision",
                    (
                        "Relative Momentum review selected "
                        f"{review.target.value}"
                    ),
                    candle_timestamp=review.candle_open_time,
                    decision_id=(
                        f"rotation:{review.candle_open_time}:"
                        f"{review.target.value}"
                    ),
                    correlation_id=(
                        f"paper-cycle:{self.config.session_id}:{received_at}"
                    ),
                )
            actual = self._actual_target()
            if review.target.value != actual:
                self._pending = PendingRotation(
                    target=review.target.value,
                    source_candle_open_time=review.candle_open_time,
                    signal_available_time=review.signal_available_time,
                    phase=RotationPhase.SELL,
                    reason=(
                        "weekly relative-momentum target changed from "
                        f"{actual} to {review.target.value}; "
                        f"btc={review.btc_momentum}, "
                        f"eth={review.eth_momentum}"
                    ),
                )
        if self._pending is None:
            self._rotation_target = self._actual_target()
            self._observe_hold_risk(pair, received_at)
            self._commit_state(candles=candle_payloads)
            return

        observations = self._observe_prices(received_at)
        committed = self._execute_pending(
            observations,
            candle_payloads=candle_payloads,
        )
        if not committed:
            self._commit_state(candles=candle_payloads)

    def _resume_pending(self, received_at: int) -> int:
        if self._pending is None or not self._candles[self.config.symbols[0]]:
            return 0
        observations = self._observe_prices(received_at)
        self._execute_pending(observations)
        return 1

    def _execute_pending(
        self,
        observations: Mapping[str, PriceObservation],
        *,
        candle_payloads: tuple[tuple[str, int, str], ...] = (),
    ) -> bool:
        pending = self._pending
        if pending is None:
            return False
        if self._open_order_count() > 0:
            # An open partial remainder retains its reservation. A second
            # intent would compete for the same funds or base quantity.
            return False
        committed = False
        active = self._active_symbol()
        if active is not None and active != pending.target:
            pending = PendingRotation(
                target=pending.target,
                source_candle_open_time=pending.source_candle_open_time,
                signal_available_time=pending.signal_available_time,
                phase=RotationPhase.SELL,
                reason=pending.reason,
            )
            self._pending = pending
            filled = self._execute_leg(
                symbol=active,
                signal=Signal.SELL,
                observation=observations[active],
                pending=pending,
            )
            self._rotation_target = self._actual_target()
            if self._accounting.base_balance(self._instruments[active]) > 0:
                self._commit_state(candles=candle_payloads)
                return True
            self._pending = PendingRotation(
                target=pending.target,
                source_candle_open_time=pending.source_candle_open_time,
                signal_available_time=pending.signal_available_time,
                phase=RotationPhase.BUY,
                reason=pending.reason,
            )
            self._commit_state(candles=candle_payloads)
            committed = True
            if filled and self._after_sell_checkpoint is not None:
                self._after_sell_checkpoint()

        pending = self._pending
        if pending is None:
            return committed
        if pending.target == RotationTarget.CASH.value:
            if self._active_symbol() is None:
                self._rotation_target = RotationTarget.CASH.value
                self._pending = None
                self._commit_state(
                    candles=() if committed else candle_payloads
                )
                return True
            return committed
        active = self._active_symbol()
        if active is not None:
            if active == pending.target:
                self._rotation_target = active
                self._pending = None
                self._commit_state(
                    candles=() if committed else candle_payloads
                )
                return True
            return committed
        self._pending = PendingRotation(
            target=pending.target,
            source_candle_open_time=pending.source_candle_open_time,
            signal_available_time=pending.signal_available_time,
            phase=RotationPhase.BUY,
            reason=pending.reason,
        )
        filled = self._execute_leg(
            symbol=pending.target,
            signal=Signal.BUY,
            observation=observations[pending.target],
            pending=self._pending,
        )
        if filled and self._active_symbol() == pending.target:
            self._rotation_target = pending.target
            self._pending = None
        self._commit_state(candles=() if committed else candle_payloads)
        return True

    def _execute_leg(
        self,
        *,
        symbol: str,
        signal: Signal,
        observation: PriceObservation,
        pending: PendingRotation,
    ) -> bool:
        if not self._reconciled:
            return False
        instrument = self._instruments[symbol]
        row = self._row_at(symbol, pending.source_candle_open_time)
        decision = make_decision(
            strategy_name="btc_eth_relative_momentum",
            symbol=symbol,
            interval=self.config.interval,
            row=row,
            signal=signal,
            explanation=pending.reason,
            diagnostics={"rotation_target": pending.target},
        )
        portfolio = self._portfolio_state(instrument)
        estimated_price = observation.price * (
            ONE + self.backtest_config.slippage_rate
            if signal is Signal.BUY
            else ONE - self.backtest_config.slippage_rate
        )
        sizing = self.position_sizer.size(SizingContext(
            decision=decision,
            instrument=instrument,
            portfolio=portfolio,
            market_open_price=observation.price,
            estimated_execution_price=estimated_price,
            observed_at=observation.observed_at,
            fee_rate=self.backtest_config.fee_rate,
            slippage_rate=self.backtest_config.slippage_rate,
        ))
        risk = self.risk_manager.check(ReplayRiskContext(
            decision=decision,
            sizing=sizing,
            portfolio=portfolio,
            instrument=instrument,
            market_open_price=observation.price,
            observed_at=observation.observed_at,
            candle_close_time=row.close_time,
            data_received_at=observation.observed_at,
            reserved_quote=self._reserved_quote(),
            open_order_count=self._open_order_count(),
            equity=self._equity(),
            execution_state_known=self._reconciled,
        ))
        if not risk.accepted or isinstance(sizing, SizingRejection):
            LOGGER.warning(
                "Portfolio paper %s rejected for %s: %s",
                signal.value,
                symbol,
                risk.reason,
            )
            if self.telemetry is not None:
                self.telemetry.event(
                    "risk_rejection",
                    f"{signal.value} {symbol} rejected: {risk.reason}",
                    dedup_key=(
                        f"risk:{pending.source_candle_open_time}:"
                        f"{signal.value}:{symbol}:{risk.reason}"
                    ),
                    notify=True,
                    symbol=symbol,
                    candle_timestamp=pending.source_candle_open_time,
                    reason=risk.reason,
                )
            return False
        if signal is Signal.BUY:
            if not isinstance(sizing, BuySizingProposal):
                raise RuntimeError("BUY produced invalid sizing")
            quantity = floor_to_step(
                sizing.quote_budget
                / (ONE + self.backtest_config.fee_rate)
                / estimated_price,
                self.execution_config.quantity_step,
            )
            available_balance = self._accounting.quote_balance
            side = OrderSide.BUY
        else:
            if not isinstance(sizing, SellSizingProposal):
                raise RuntimeError("SELL produced invalid sizing")
            quantity = floor_to_step(
                sizing.base_quantity,
                self.execution_config.quantity_step,
            )
            available_balance = self._accounting.base_balance(instrument)
            side = OrderSide.SELL
        if quantity <= 0:
            return False
        attempt = len(self._adapter.orders) + 1
        intent = OrderIntent(
            intent_id=(
                f"paper-rotation:{self.config.session_id}:"
                f"{pending.source_candle_open_time}:{side.value}:"
                f"{symbol}:{attempt}"
            ),
            source_event_id=(
                f"rotation:{pending.source_candle_open_time}:"
                f"{side.value}:{symbol}:{attempt}"
            ),
            source=IntentSource.STRATEGY,
            instrument=instrument,
            side=side,
            quantity=quantity,
            created_at=observation.observed_at,
            earliest_execution_at=observation.observed_at,
            signal_available_time=pending.signal_available_time,
            source_candle_open_time=pending.source_candle_open_time,
            explanation=pending.reason,
        )
        try:
            result = self._simulator.submit_and_simulate(
                intent,
                observed_price=observation.price,
                observed_at=observation.observed_at,
                fee_rate=self.backtest_config.fee_rate,
                slippage_rate=self.backtest_config.slippage_rate,
                available_balance=available_balance,
            )
        except ValueError as error:
            LOGGER.warning("Portfolio paper order rejected: %s", error)
            return False
        if result.fill_applied:
            PaperExecutionSimulator.apply_fill_to_accounting(
                self._accounting,
                result.fill,
            )
            self._accounting.assert_invariants()
        return result.fill_applied

    def _review_for_latest_pair(self):
        btc_rows = self._rows["BTCUSDT"]
        eth_rows = self._rows["ETHUSDT"]
        latest_index = len(btc_rows) - 1
        if latest_index < self._planner.lookback_candles or (
            (latest_index - self._planner.lookback_candles)
            % self._planner.rebalance_candles
        ):
            return None
        plan = self._planner.build(btc_rows, eth_rows)
        latest_open = btc_rows[-1].open_time
        return next(
            (
                review
                for review in reversed(plan.reviews)
                if review.candle_open_time == latest_open
            ),
            None,
        )

    def _observe_prices(
        self,
        received_at: int,
    ) -> dict[str, PriceObservation]:
        observations = {
            symbol: self.market_source.observe_price(symbol)
            for symbol in self.config.symbols
        }
        for symbol, observation in observations.items():
            if observation.observed_at < received_at:
                raise ValueError(
                    f"Observed {symbol} price predates synchronized receipt"
                )
            self._latest_prices[symbol] = observation.price
        return observations

    def _observe_hold_risk(
        self,
        pair: Mapping[str, Candle],
        received_at: int,
    ) -> None:
        symbol = self.config.symbols[0]
        row = self._rows[symbol][-1]
        decision = make_decision(
            strategy_name="btc_eth_relative_momentum",
            symbol=symbol,
            interval=self.config.interval,
            row=row,
            signal=Signal.HOLD,
            explanation="No executable synchronized rotation",
        )
        self.risk_manager.check(ReplayRiskContext(
            decision=decision,
            sizing=None,
            portfolio=self._portfolio_state(self._instruments[symbol]),
            instrument=self._instruments[symbol],
            market_open_price=pair[symbol].close,
            observed_at=received_at,
            candle_close_time=pair[symbol].close_time,
            data_received_at=received_at,
            reserved_quote=self._reserved_quote(),
            open_order_count=self._open_order_count(),
            equity=self._equity(),
            execution_state_known=self._reconciled,
        ))

    def _build_reconciliation(
        self,
        checked_at: int,
    ) -> PortfolioReconciliationReport:
        diagnostics = list(self._integrity_faults)
        try:
            rebuilt = self._accounting_from_fills(self._adapter.fills)
            if rebuilt.quote_balance != self._accounting.quote_balance:
                diagnostics.append("cash differs from fill-derived accounting")
            if rebuilt.all_lots() != self._accounting.all_lots():
                diagnostics.append("FIFO lots differ from fill-derived accounting")
            if rebuilt.realizations != self._accounting.realizations:
                diagnostics.append(
                    "realized PnL differs from fill-derived accounting"
                )
            if rebuilt.fees_paid != self._accounting.fees_paid:
                diagnostics.append("fees differ from execution fills")
            snapshot = self._adapter.snapshot()
            restored = SimulatedExecutionAdapter.from_snapshot(snapshot)
            roundtrip = self._adapter.reconcile(restored.snapshot())
            if not roundtrip.is_consistent:
                diagnostics.append("execution snapshot round-trip failed")
            if len(snapshot.lifecycle.fills) != len(
                {item.fill_id for item in snapshot.lifecycle.fills}
            ):
                diagnostics.append("duplicate fill identifier")
            reservations = {
                item.order_id: item
                for item in snapshot.lifecycle.reservations
            }
            for order in snapshot.lifecycle.orders:
                reservation = reservations.get(order.order_id)
                if reservation is None:
                    diagnostics.append(
                        f"order {order.order_id} has no reservation"
                    )
                    continue
                order_fills = [
                    fill
                    for fill in snapshot.lifecycle.fills
                    if fill.order_id == order.order_id
                ]
                consumed = sum(
                    (
                        fill.quote_notional + fill.fee
                        if order.side is OrderSide.BUY
                        else fill.quantity
                        for fill in order_fills
                    ),
                    ZERO,
                )
                expected_remaining = (
                    ZERO
                    if order.status.is_terminal
                    else reservation.original_amount - consumed
                )
                if reservation.remaining_amount != expected_remaining:
                    diagnostics.append(
                        f"reservation for {order.order_id} does not reconcile"
                    )
            if self._reserved_quote() > self._accounting.quote_balance:
                diagnostics.append("reserved USDT exceeds available cash")
            for symbol, instrument in self._instruments.items():
                reserved_base = sum(
                    (
                        item.remaining_amount
                        for item in snapshot.lifecycle.reservations
                        if item.asset == instrument.base_asset
                    ),
                    ZERO,
                )
                if reserved_base > self._accounting.base_balance(instrument):
                    diagnostics.append(
                        f"reserved {symbol} exceeds the open position"
                    )
            self._accounting.assert_invariants()
            active = [
                symbol
                for symbol, instrument in self._instruments.items()
                if self._accounting.base_balance(instrument) > 0
            ]
            if len(active) > 1:
                diagnostics.append("more than one Rotation asset is active")
            if (
                self._pending is None
                and self._rotation_target != self._actual_target()
            ):
                diagnostics.append(
                    "rotation target differs from actual portfolio holdings"
                )
            if self._sequence:
                lengths = {len(items) for items in self._candles.values()}
                if lengths != {self._sequence}:
                    diagnostics.append(
                        "processed candle timestamps are not synchronized"
                    )
                elif any(
                    left.open_time != right.open_time
                    for left, right in zip(
                        self._candles["BTCUSDT"],
                        self._candles["ETHUSDT"],
                    )
                ):
                    diagnostics.append(
                        "processed BTC/ETH candle timestamps differ"
                    )
        except Exception as error:
            diagnostics.append(f"reconciliation error: {error}")
        unrealized = ZERO
        try:
            unrealized = self._accounting.unrealized_pnl(
                self._mark_prices_for_open_positions()
            )
        except ValueError as error:
            diagnostics.append(str(error))
        return PortfolioReconciliationReport(
            checked_at=checked_at,
            consistent=not diagnostics,
            diagnostics=tuple(diagnostics),
            quote_balance=self._accounting.quote_balance,
            btc_balance=self._accounting.base_balance(
                self._instruments["BTCUSDT"]
            ),
            eth_balance=self._accounting.base_balance(
                self._instruments["ETHUSDT"]
            ),
            realized_pnl=self._accounting.realized_pnl,
            unrealized_pnl=unrealized,
            fees_paid=self._accounting.fees_paid,
            order_count=len(self._adapter.orders),
            fill_count=len(self._adapter.fills),
            reservation_count=len(self._adapter.reservations),
            checkpoint_sequence=self._sequence,
            last_synchronized_open_time=(
                None if self._sequence == 0 else self._last_open_time()
            ),
        )

    def _commit_state(
        self,
        *,
        candles: tuple[tuple[str, int, str], ...] = (),
        report: PortfolioReconciliationReport | None = None,
    ) -> None:
        previous_sequence = self._sequence
        if candles:
            self._sequence += 1
        report = report or self._build_reconciliation(self._now_ms())
        self._reconciled = report.consistent
        self._diagnostics = report.diagnostics
        try:
            self._revision = self.repository.commit_portfolio_state(
                session_id=self.config.session_id,
                checkpoint_json=self._checkpoint_json(),
                execution_json=serialize_execution_snapshot(
                    self._adapter.snapshot()
                ),
                risk_state_json=canonical_json(
                    self.risk_manager.snapshot_state()
                ),
                reconciled=report.consistent,
                diagnostics_json=canonical_json({
                    "diagnostics": list(report.diagnostics)
                }),
                reconciliation_json=canonical_json(report),
                reconciliation_checked_at=report.checked_at,
                candles=candles,
            )
        except Exception:
            self._sequence = previous_sequence
            raise

    def _configuration_json(self) -> str:
        return canonical_json({
            "kind": "btc_eth_rotation_paper_v1",
            "exchange": self.config.exchange,
            "market_type": self.config.market_type,
            "symbols": list(self.config.symbols),
            "interval": self.config.interval,
            "warmup_candles": self.config.warmup_candles,
            "initial_cash": self.backtest_config.initial_cash,
            "fee_rate": self.backtest_config.fee_rate,
            "slippage_rate": self.backtest_config.slippage_rate,
            "position_sizer": self.position_sizer.name,
            "momentum_lookback_candles": self._planner.lookback_candles,
            "rebalance_candles": self._planner.rebalance_candles,
            "risk": dict(self.risk_manager.persistence_configuration()),
            "execution": {
                "quantity_step": self.execution_config.quantity_step,
                "minimum_quantity": self.execution_config.minimum_quantity,
                "minimum_notional": self.execution_config.minimum_notional,
                "simulated_latency_ms": (
                    self.execution_config.simulated_latency_ms
                ),
                "partial_fill_fraction": (
                    self.execution_config.partial_fill_fraction
                ),
                "cancel_unfilled_remainder": (
                    self.execution_config.cancel_unfilled_remainder
                ),
            },
        })

    def _checkpoint_json(self) -> str:
        pending = self._pending
        return canonical_json({
            "kind": "btc_eth_rotation_checkpoint_v1",
            "rotation_target": self._rotation_target,
            "pending": (
                None
                if pending is None
                else {
                    "target": pending.target,
                    "source_candle_open_time": (
                        pending.source_candle_open_time
                    ),
                    "signal_available_time": pending.signal_available_time,
                    "phase": pending.phase.value,
                    "reason": pending.reason,
                }
            ),
            "latest_prices": dict(self._latest_prices),
            "quote_balance": self._accounting.quote_balance,
            "btc_balance": self._accounting.base_balance(
                self._instruments["BTCUSDT"]
            ),
            "eth_balance": self._accounting.base_balance(
                self._instruments["ETHUSDT"]
            ),
            "realized_pnl": self._accounting.realized_pnl,
            "fees_paid": self._accounting.fees_paid,
            "candle_journal_digest": self._candle_journal_digest(),
        })

    def _restore_checkpoint(self, raw: Mapping[str, object]) -> None:
        if raw.get("kind") != "btc_eth_rotation_checkpoint_v1":
            raise ReplayStateIntegrityError(
                "Unsupported portfolio checkpoint kind"
            )
        target = raw.get("rotation_target")
        if target not in {"USDT", "BTCUSDT", "ETHUSDT"}:
            raise ReplayStateIntegrityError("Invalid stored rotation target")
        self._rotation_target = str(target)
        pending = raw.get("pending")
        if pending is None:
            self._pending = None
        elif isinstance(pending, dict):
            self._pending = PendingRotation(
                target=str(pending["target"]),
                source_candle_open_time=int(
                    pending["source_candle_open_time"]
                ),
                signal_available_time=int(pending["signal_available_time"]),
                phase=RotationPhase(str(pending["phase"])),
                reason=str(pending["reason"]),
            )
        else:
            raise ReplayStateIntegrityError("Invalid stored pending rotation")
        prices = raw.get("latest_prices")
        if not isinstance(prices, dict):
            raise ReplayStateIntegrityError("Invalid stored mark prices")
        self._latest_prices = {
            str(symbol): value
            for symbol, value in prices.items()
            if isinstance(value, Decimal)
        }
        expected = {
            "quote_balance": self._accounting.quote_balance,
            "btc_balance": self._accounting.base_balance(
                self._instruments["BTCUSDT"]
            ),
            "eth_balance": self._accounting.base_balance(
                self._instruments["ETHUSDT"]
            ),
            "realized_pnl": self._accounting.realized_pnl,
            "fees_paid": self._accounting.fees_paid,
        }
        for name, value in expected.items():
            if raw.get(name) != value:
                self._integrity_faults += (
                    f"stored {name} differs from fill-derived accounting",
                )
        if raw.get("candle_journal_digest") != self._candle_journal_digest():
            self._integrity_faults += (
                "stored candle journal digest does not match OHLCV history",
            )

    def _accounting_from_fills(
        self,
        fills: Sequence,
    ) -> PortfolioAccounting:
        accounting = PortfolioAccounting(self.backtest_config.initial_cash)
        for fill in fills:
            PaperExecutionSimulator.apply_fill_to_accounting(accounting, fill)
        accounting.assert_invariants()
        return accounting

    def _reset_runtime_state(self) -> None:
        """Forget mutable memory so recovery starts at the durable checkpoint."""
        self._candles = {symbol: [] for symbol in self.config.symbols}
        self._rows = {symbol: [] for symbol in self.config.symbols}
        self._latest_prices = {}
        self._accounting = PortfolioAccounting(
            self.backtest_config.initial_cash
        )
        self._adapter = SimulatedExecutionAdapter()
        self._simulator = PaperExecutionSimulator(
            self.execution_config,
            adapter=self._adapter,
        )
        self._pending = None
        self._rotation_target = RotationTarget.CASH.value
        self._sequence = 0
        self._revision = 0
        self._reconciled = True
        self._diagnostics = ()
        self._integrity_faults = ()

    def _validate_synchronized_range(
        self,
        loaded: Mapping[str, Sequence[Candle]],
        start_time: int,
        end_time: int,
        received_at: int,
    ) -> None:
        interval_ms = get_interval_milliseconds(self.config.interval)
        expected = (end_time - start_time) // interval_ms
        for symbol in self.config.symbols:
            candles = loaded[symbol]
            if len(candles) != expected:
                raise ValueError(
                    f"{symbol} returned {len(candles)} candles; "
                    f"expected {expected}; synchronized set is incomplete"
                )
            if not candles or candles[0].open_time != start_time:
                raise ValueError(f"{symbol} did not start at requested boundary")
            if candles[-1].open_time != end_time - interval_ms:
                raise ValueError(f"{symbol} is delayed at range end")
            validate_historical_candles(
                candles,
                self.config.interval,
                as_of_time=received_at,
            )
        for index, (btc, eth) in enumerate(zip(
            loaded["BTCUSDT"], loaded["ETHUSDT"]
        )):
            if (btc.open_time, btc.close_time) != (
                eth.open_time,
                eth.close_time,
            ):
                raise ValueError(
                    f"BTC/ETH candle set is misaligned at index {index}"
                )

    def _rebuild_features(self) -> None:
        for symbol in self.config.symbols:
            candles = self._candles[symbol]
            self._rows[symbol] = (
                []
                if not candles
                else self._builder.build(
                    candles,
                    interval=self.config.interval,
                    as_of_time=candles[-1].close_time + 1,
                )
            )

    def _row_at(self, symbol: str, open_time: int) -> FeatureRow:
        for row in reversed(self._rows[symbol]):
            if row.open_time == open_time:
                return row
        raise ReplayStateIntegrityError(
            "Pending rotation source candle is absent from history"
        )

    def _actual_target(self) -> str:
        active = self._active_symbol()
        return RotationTarget.CASH.value if active is None else active

    def _active_symbol(self) -> str | None:
        active = [
            symbol
            for symbol, instrument in self._instruments.items()
            if self._accounting.base_balance(instrument) > 0
        ]
        if len(active) > 1:
            raise ReplayStateIntegrityError(
                "Rotation portfolio contains multiple active assets"
            )
        return None if not active else active[0]

    def _portfolio_state(self, instrument: Instrument) -> PortfolioState:
        return PortfolioState(
            quote_balance=self._accounting.quote_balance,
            base_balance=self._accounting.base_balance(instrument),
            entry_cost=self._accounting.cost_basis(instrument),
            realized_pnl=self._accounting.realized_pnl,
            fees_paid=self._accounting.fees_paid,
        )

    def _mark_prices_for_open_positions(self) -> dict[Instrument, Decimal]:
        return {
            instrument: self._latest_prices[symbol]
            for symbol, instrument in self._instruments.items()
            if self._accounting.base_balance(instrument) > 0
            and symbol in self._latest_prices
        }

    def _equity(self) -> Decimal:
        return self._accounting.equity(
            self._mark_prices_for_open_positions()
        )

    def _reserved_quote(self) -> Decimal:
        return sum(
            (
                item.remaining_amount
                for item in self._adapter.reservations
                if item.asset == "USDT"
            ),
            ZERO,
        )

    def _open_order_count(self) -> int:
        return sum(not item.status.is_terminal for item in self._adapter.orders)

    def _last_open_time(self) -> int:
        return self._candles[self.config.symbols[0]][-1].open_time

    def _candle_journal_digest(self) -> str:
        digest = sha256()
        for index in range(len(self._candles[self.config.symbols[0]])):
            for symbol in self.config.symbols:
                digest.update(symbol.encode("utf-8"))
                digest.update(b"\0")
                digest.update(
                    serialize_candle(self._candles[symbol][index]).encode(
                        "utf-8"
                    )
                )
                digest.update(b"\0")
        return digest.hexdigest()

    def _cycle_result(
        self,
        *,
        checked_at: int,
        range_start: int | None,
        range_end: int | None,
        processed: int,
        backfilled: int,
        live: int,
        fills_before: int,
        report: PortfolioReconciliationReport,
        message: str,
    ) -> MultiSymbolPaperCycleResult:
        return MultiSymbolPaperCycleResult(
            session_id=self.config.session_id,
            checked_at=checked_at,
            range_start=range_start,
            range_end=range_end,
            processed_candle_sets=processed,
            backfilled_candle_sets=backfilled,
            live_candle_sets=live,
            fills_created=len(self._adapter.fills) - fills_before,
            rotation_target=self._rotation_target,
            pending_target=(
                None if self._pending is None else self._pending.target
            ),
            trading_paused=self.risk_manager.paused,
            reconciled=report.consistent,
            message=message,
        )

    def _observe_telemetry(
        self,
        report: PortfolioReconciliationReport,
        checked_at: int,
        *,
        result: MultiSymbolPaperCycleResult | None = None,
    ) -> None:
        if self.telemetry is None:
            return
        correlation_id = (
            f"paper-cycle:{self.config.session_id}:{checked_at}"
        )
        self.telemetry.execution(
            self._adapter.orders,
            self._adapter.fills,
            correlation_id=correlation_id,
        )
        risk_state = self.risk_manager.snapshot_state()
        interval_ms = get_interval_milliseconds(self.config.interval)
        last_open = (
            None if self._sequence == 0 else self._last_open_time()
        )
        self.telemetry.portfolio(
            cash=report.quote_balance,
            equity=(
                self.backtest_config.initial_cash
                + report.realized_pnl
                + report.unrealized_pnl
            ),
            paused=self.risk_manager.paused,
            last_candle_open_time=last_open,
            decision_available_time=(
                None if last_open is None else last_open + interval_ms
            ),
            interval_ms=interval_ms,
            risk_rejections=int(risk_state["rejections"]),
            reconciled=report.consistent,
            correlation_id=correlation_id,
            peak_equity=risk_state["peak_equity"],
        )
        if (
            result is not None
            and result.processed_candle_sets
            and result.live_candle_sets == 0
        ):
            self.telemetry.stale(
                candle_open_time=last_open,
                correlation_id=correlation_id,
            )
        self.telemetry.flush()

    def _status(
        self,
        report: PortfolioReconciliationReport,
    ) -> MultiSymbolPaperSessionStatus:
        return MultiSymbolPaperSessionStatus(
            session_id=self.config.session_id,
            checkpoint_sequence=self._sequence,
            checkpoint_revision=self._revision,
            checkpoint_open_time=(
                None if self._sequence == 0 else self._last_open_time()
            ),
            quote_balance=report.quote_balance,
            btc_balance=report.btc_balance,
            eth_balance=report.eth_balance,
            realized_pnl=report.realized_pnl,
            unrealized_pnl=report.unrealized_pnl,
            fees_paid=report.fees_paid,
            order_count=report.order_count,
            fill_count=report.fill_count,
            reservation_count=report.reservation_count,
            rotation_target=self._rotation_target,
            pending_target=(
                None if self._pending is None else self._pending.target
            ),
            pending_phase=(
                None if self._pending is None else self._pending.phase.value
            ),
            trading_paused=self.risk_manager.paused,
            reconciled=report.consistent,
            reconciliation_diagnostics=report.diagnostics,
        )


def set_portfolio_paper_trading_pause(
    database_path: str | Path,
    session_id: str,
    paused: bool,
) -> int:
    repository = PaperStateRepository(database_path)
    repository.init_schema()
    return repository.set_portfolio_trading_paused(session_id, paused)
