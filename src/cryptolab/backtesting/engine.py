"""Deterministic long-only Spot backtesting engine."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from cryptolab.backtesting.accounting import (
    LotRealization,
    PortfolioAccounting,
    PositionCycle,
    PositionLot,
)
from cryptolab.backtesting.models import (
    BacktestConfig,
    BacktestReport,
    CompletedTrade,
    EquityPoint,
    Fill,
    PortfolioState,
    ProtectiveStopFill,
    ProtectiveStopTriggerKind,
    SimulatedFill,
    SkippedDecision,
    TimedExitFill,
    TrailingStopFill,
    TrailingStopTriggerKind,
)
from cryptolab.backtesting.sizing import (
    BuySizingProposal,
    LegacyAllInAllOutSizer,
    PositionSizer,
    SellSizingProposal,
    SizingContext,
    SizingRejection,
)
from cryptolab.backtesting.portfolio import (
    PortfolioBacktestInput,
    PortfolioBacktestReport,
    PortfolioEquityPoint,
    PositionValue,
)
from cryptolab.execution.models import (
    Fill as ExecutionFill,
    Instrument,
    IntentSource,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from cryptolab.execution.adapter import (
    ExecutionAdapter,
    ExecutionEvent,
    SimulatedExecutionAdapter,
)
from cryptolab.execution.lifecycle import BalanceReservation
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.markets import (
    DEFAULT_EXCHANGE,
    SPOT_MARKET_TYPE,
    validate_market_identity,
)
from cryptolab.strategies.base import Strategy
from cryptolab.strategies.models import Signal, StrategyDecision
from cryptolab.strategies.runner import StrategyRunner


@dataclass(frozen=True)
class _TrailingStopState:
    """High watermark known before the candle currently being processed."""

    high_watermark: Decimal


@dataclass(frozen=True)
class _ProtectiveStopState:
    """Fixed stop anchored to the actual entry execution price."""

    entry_execution_price: Decimal


@dataclass(frozen=True)
class _TimedExitState:
    """Actual entry event used to schedule a position-level time exit."""

    entry_execution_open_time: int


@dataclass(frozen=True)
class StrategyExecutionOutcome:
    """One strategy decision processed by the shared execution kernel."""

    state: PortfolioState
    fill: SimulatedFill | None
    completed_trades: tuple[CompletedTrade, ...]
    skipped_decision: SkippedDecision | None
    order_intent: OrderIntent | None


@dataclass(frozen=True)
class BacktestExecutionCheckpoint:
    """Immutable incremental execution state for persistence and audit."""

    state: PortfolioState
    fills: tuple[SimulatedFill, ...]
    completed_trades: tuple[CompletedTrade, ...]
    skipped_decisions: tuple[SkippedDecision, ...]
    order_intents: tuple[OrderIntent, ...]
    orders: tuple[Order, ...]
    execution_fills: tuple[ExecutionFill, ...]
    reservations: tuple[BalanceReservation, ...]
    execution_events: tuple[ExecutionEvent, ...]
    open_lots: tuple[PositionLot, ...]
    lot_realizations: tuple[LotRealization, ...]
    position_cycles: tuple[PositionCycle, ...]


class BacktestEngine:
    """Evaluate one strategy with one independent virtual Spot portfolio."""

    def __init__(
        self,
        config: BacktestConfig | None = None,
        *,
        position_sizer: PositionSizer | None = None,
    ) -> None:
        self._config = config or BacktestConfig()
        self._position_sizer = position_sizer or LegacyAllInAllOutSizer()
        if (
            not isinstance(self._position_sizer.name, str)
            or not self._position_sizer.name.strip()
        ):
            raise ValueError("Position sizer name must be a non-empty string")
        if (
            self._config.stop_loss_rate is not None
            and getattr(self._position_sizer, "allows_scale_in", False)
        ):
            raise ValueError(
                "stop_loss_rate does not support scale-in position sizing"
            )
        if (
            self._config.max_holding_candles is not None
            and getattr(self._position_sizer, "allows_scale_in", False)
        ):
            raise ValueError(
                "max_holding_candles does not support scale-in position sizing"
            )

    def start_execution_session(
        self,
        instrument: Instrument,
    ) -> "BacktestExecutionSession":
        """Create an incremental single-instrument strategy execution state.

        Stage 1 replay deliberately supports normal strategy decisions only;
        candle-path stop models remain owned by the batch backtest loop.
        """
        if not isinstance(instrument, Instrument):
            raise ValueError("instrument must be an Instrument")
        if any((
            self._config.trailing_stop_rate is not None,
            self._config.stop_loss_rate is not None,
            self._config.max_holding_candles is not None,
        )):
            raise ValueError(
                "Incremental strategy execution does not support stop or "
                "timed-exit configuration"
            )
        return BacktestExecutionSession(self, instrument)

    def run(
        self,
        strategy: Strategy,
        symbol: str,
        interval: str,
        rows: Sequence[FeatureRow],
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
        as_of_time: int | None = None,
    ) -> BacktestReport:
        """Run a look-ahead-safe backtest over end-of-candle features."""
        validate_market_identity(exchange, market_type)
        if not symbol.endswith("USDT"):
            raise ValueError("Backtesting MVP requires a USDT-quoted symbol")
        if not rows:
            raise ValueError("At least one feature row is required")

        instrument = Instrument(
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            base_asset=symbol.removesuffix("USDT"),
            quote_asset="USDT",
        )

        decisions = StrategyRunner([strategy]).run(
            symbol,
            interval,
            rows,
            as_of_time=as_of_time,
        )
        eligible_rows = tuple(
            row
            for row in rows
            if as_of_time is None or row.available_time <= as_of_time
        )
        if not eligible_rows:
            raise ValueError("No feature rows are available at as_of_time")
        if len(decisions) != len(eligible_rows):
            raise RuntimeError("Strategy decisions are not aligned with features")

        accounting = PortfolioAccounting(self._config.initial_cash)
        lifecycle = SimulatedExecutionAdapter()
        state = self._portfolio_state(accounting, instrument)
        fills: list[Fill] = []
        trades: list[CompletedTrade] = []
        skipped: list[SkippedDecision] = []
        equity_curve: list[EquityPoint] = []
        order_intents: list[OrderIntent] = []
        intent_ids: set[str] = set()
        source_event_ids: set[str] = set()
        fills_by_id: dict[str, SimulatedFill] = {}
        trailing_stop_state: _TrailingStopState | None = None
        protective_stop_state: _ProtectiveStopState | None = None
        timed_exit_state: _TimedExitState | None = None

        for index, row in enumerate(eligible_rows):
            if index > 0:
                decision = decisions[index - 1]
                state, fill, new_trades, skip, intent = self._execute_decision(
                    decision,
                    row,
                    state,
                    instrument,
                    accounting,
                    fills_by_id,
                    lifecycle,
                )
                if intent is not None:
                    if intent.intent_id in intent_ids:
                        raise RuntimeError("Duplicate order intent identifier")
                    if intent.source_event_id in source_event_ids:
                        raise RuntimeError(
                            "One source event produced more than one order intent"
                        )
                    intent_ids.add(intent.intent_id)
                    source_event_ids.add(intent.source_event_id)
                    order_intents.append(intent)
                if fill is not None:
                    fills.append(fill)
                    if fill.side is Signal.BUY:
                        if not isinstance(fill, SimulatedFill):
                            raise RuntimeError(
                                "A trailing-stop fill cannot open a position"
                            )
                        if fill.fill_id is None:
                            raise RuntimeError("Strategy fill has no fill identifier")
                        fills_by_id[fill.fill_id] = fill
                        if (
                            self._config.trailing_stop_rate is not None
                            and trailing_stop_state is None
                        ):
                            trailing_stop_state = _TrailingStopState(
                                high_watermark=fill.execution_price,
                            )
                        if self._config.stop_loss_rate is not None:
                            if protective_stop_state is not None:
                                raise RuntimeError(
                                    "Protective stop already has an entry state"
                                )
                            protective_stop_state = _ProtectiveStopState(
                                entry_execution_price=fill.execution_price,
                            )
                        if self._config.max_holding_candles is not None:
                            if timed_exit_state is not None:
                                raise RuntimeError(
                                    "Timed exit already has an entry state"
                                )
                            timed_exit_state = _TimedExitState(
                                entry_execution_open_time=(
                                    fill.execution_open_time
                                ),
                            )
                    elif state.base_balance == 0:
                        trailing_stop_state = None
                        protective_stop_state = None
                        timed_exit_state = None
                trades.extend(new_trades)
                if skip is not None:
                    skipped.append(skip)

            if (
                self._config.max_holding_candles is not None
                and state.base_balance > 0
            ):
                if timed_exit_state is None:
                    raise RuntimeError("Open position has no timed-exit state")
                timed_result = self._execute_timed_exit(
                    row,
                    state,
                    timed_exit_state,
                    instrument,
                    accounting,
                    fills_by_id,
                    lifecycle,
                )
                if timed_result is not None:
                    state, timed_fill, timed_trades, timed_intent = timed_result
                    if timed_intent.intent_id in intent_ids:
                        raise RuntimeError("Duplicate timed-exit intent")
                    if timed_intent.source_event_id in source_event_ids:
                        raise RuntimeError("Duplicate timed-exit source event")
                    intent_ids.add(timed_intent.intent_id)
                    source_event_ids.add(timed_intent.source_event_id)
                    order_intents.append(timed_intent)
                    fills.append(timed_fill)
                    trades.extend(timed_trades)
                    timed_exit_state = None
                    trailing_stop_state = None
                    protective_stop_state = None

            if (
                self._config.trailing_stop_rate is not None
                and state.base_balance > 0
            ):
                if trailing_stop_state is None:
                    raise RuntimeError(
                        "Open position has no trailing-stop state"
                    )
                stop_result = self._execute_trailing_stop(
                    row,
                    state,
                    trailing_stop_state,
                    instrument,
                    accounting,
                    fills_by_id,
                    lifecycle,
                )
                if stop_result is not None:
                    state, stop_fill, stop_trades, stop_intent = stop_result
                    if stop_intent.intent_id in intent_ids:
                        raise RuntimeError("Duplicate trailing-stop intent")
                    intent_ids.add(stop_intent.intent_id)
                    source_event_ids.add(stop_intent.source_event_id)
                    order_intents.append(stop_intent)
                    fills.append(stop_fill)
                    trades.extend(stop_trades)
                    trailing_stop_state = None
                    protective_stop_state = None
                    timed_exit_state = None
                else:
                    trailing_stop_state = _TrailingStopState(
                        high_watermark=max(
                            trailing_stop_state.high_watermark,
                            row.high,
                        ),
                    )

            if (
                self._config.stop_loss_rate is not None
                and state.base_balance > 0
            ):
                if protective_stop_state is None:
                    raise RuntimeError(
                        "Open position has no protective-stop state"
                    )
                stop_result = self._execute_protective_stop(
                    row,
                    state,
                    protective_stop_state,
                    instrument,
                    accounting,
                    fills_by_id,
                    lifecycle,
                )
                if stop_result is not None:
                    state, stop_fill, stop_trades, stop_intent = stop_result
                    if stop_intent.intent_id in intent_ids:
                        raise RuntimeError("Duplicate protective-stop intent")
                    if stop_intent.source_event_id in source_event_ids:
                        raise RuntimeError(
                            "Duplicate protective-stop source event"
                        )
                    intent_ids.add(stop_intent.intent_id)
                    source_event_ids.add(stop_intent.source_event_id)
                    order_intents.append(stop_intent)
                    fills.append(stop_fill)
                    trades.extend(stop_trades)
                    protective_stop_state = None
                    trailing_stop_state = None
                    timed_exit_state = None

            equity_curve.append(self._mark_equity(row, state))

        last_decision = decisions[-1]
        if last_decision.signal is not Signal.HOLD:
            skipped.append(SkippedDecision(
                decision=last_decision,
                reason="no next candle is available for execution",
            ))

        last_close = eligible_rows[-1].close
        final_equity = accounting.equity({instrument: last_close})
        total_pnl = final_equity - self._config.initial_cash
        unrealized_pnl = total_pnl - state.realized_pnl
        self._assert_accounting_invariants(
            state,
            fills,
            trades,
            equity_curve,
            order_intents,
            accounting,
            instrument,
            lifecycle,
            final_equity,
            total_pnl,
            unrealized_pnl,
        )

        return BacktestReport(
            strategy_name=strategy.name,
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            interval=interval,
            config=self._config,
            final_state=state,
            final_equity=final_equity,
            realized_pnl=state.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_pnl=total_pnl,
            maximum_equity_drawdown=self._maximum_drawdown(equity_curve),
            fills=tuple(fills),
            completed_trades=tuple(trades),
            skipped_decisions=tuple(skipped),
            equity_curve=tuple(equity_curve),
            position_sizer_name=self._position_sizer.name,
            order_intents=tuple(order_intents),
            open_lots=accounting.all_lots(),
            lot_realizations=accounting.realizations,
            position_cycles=accounting.cycles,
            orders=lifecycle.orders,
            execution_fills=lifecycle.fills,
        )

    def run_portfolio(
        self,
        inputs: Sequence[PortfolioBacktestInput],
        *,
        as_of_time: int | None = None,
    ) -> PortfolioBacktestReport:
        """Run aligned instruments against one shared USDT cash balance.

        All streams must use the same interval and timestamp grid. At one
        candle open, SELL events release shared quote capital before BUY
        events consume it; events on the same side retain stable
        instrument/strategy order. This makes cross-asset rotations explicit
        and deterministic.
        """
        if not inputs:
            raise ValueError("At least one portfolio input is required")

        prepared: list[
            tuple[
                tuple[str, str, str, str],
                PortfolioBacktestInput,
                Instrument,
                tuple[FeatureRow, ...],
                tuple[StrategyDecision, ...],
            ]
        ] = []
        intervals = {item.interval for item in inputs}
        if len(intervals) != 1:
            raise ValueError("Portfolio inputs must use one common interval")
        interval = next(iter(intervals))
        seen_instruments: set[tuple[str, str, str]] = set()

        for item in inputs:
            validate_market_identity(item.exchange, item.market_type)
            if not item.symbol.endswith("USDT"):
                raise ValueError(
                    "Shared portfolio currently requires USDT-quoted symbols"
                )
            identity = (item.exchange, item.market_type, item.symbol)
            if identity in seen_instruments:
                raise ValueError(
                    "Only one strategy stream per instrument is supported"
                )
            seen_instruments.add(identity)
            instrument = Instrument(
                exchange=item.exchange,
                market_type=item.market_type,
                symbol=item.symbol,
                base_asset=item.symbol.removesuffix("USDT"),
                quote_asset="USDT",
            )
            decisions = StrategyRunner([item.strategy]).run(
                item.symbol,
                item.interval,
                item.rows,
                as_of_time=as_of_time,
            )
            rows = tuple(
                row
                for row in item.rows
                if as_of_time is None or row.available_time <= as_of_time
            )
            if not rows:
                raise ValueError(
                    f"No feature rows are available for {item.symbol}"
                )
            if len(decisions) != len(rows):
                raise RuntimeError(
                    f"Decisions are not aligned for {item.symbol}"
                )
            sort_key = (*identity, item.strategy.name)
            prepared.append((sort_key, item, instrument, rows, decisions))

        prepared.sort(key=lambda item: item[0])
        reference_timeline = tuple(
            (row.open_time, row.close_time, row.available_time)
            for row in prepared[0][3]
        )
        for _, item, _, rows, _ in prepared[1:]:
            timeline = tuple(
                (row.open_time, row.close_time, row.available_time)
                for row in rows
            )
            if timeline != reference_timeline:
                raise ValueError(
                    "Portfolio feature rows must have identical timelines; "
                    f"{item.symbol} is not aligned"
                )

        accounting = PortfolioAccounting(self._config.initial_cash)
        lifecycle = SimulatedExecutionAdapter()
        fills: list[Fill] = []
        trades: list[CompletedTrade] = []
        skipped: list[SkippedDecision] = []
        intents: list[OrderIntent] = []
        intent_ids: set[str] = set()
        source_event_ids: set[str] = set()
        fills_by_id: dict[str, SimulatedFill] = {}
        trailing_states: dict[Instrument, _TrailingStopState] = {}
        protective_states: dict[Instrument, _ProtectiveStopState] = {}
        timed_exit_states: dict[Instrument, _TimedExitState] = {}
        equity_curve: list[PortfolioEquityPoint] = []

        for index in range(len(reference_timeline)):
            current_rows = {
                instrument: rows[index]
                for _, _, instrument, rows, _ in prepared
            }
            if index > 0:
                execution_order = sorted(
                    prepared,
                    key=lambda item: (
                        self._portfolio_signal_priority(
                            item[4][index - 1].signal,
                        ),
                        item[0],
                    ),
                )
                for _, _, instrument, _, decisions in execution_order:
                    row = current_rows[instrument]
                    state = self._portfolio_state(accounting, instrument)
                    state, fill, new_trades, skip, intent = (
                        self._execute_decision(
                            decisions[index - 1],
                            row,
                            state,
                            instrument,
                            accounting,
                            fills_by_id,
                            lifecycle,
                        )
                    )
                    if intent is not None:
                        if intent.intent_id in intent_ids:
                            raise RuntimeError(
                                "Duplicate order intent identifier"
                            )
                        if intent.source_event_id in source_event_ids:
                            raise RuntimeError(
                                "One source event produced multiple intents"
                            )
                        intent_ids.add(intent.intent_id)
                        source_event_ids.add(intent.source_event_id)
                        intents.append(intent)
                    if fill is not None:
                        fills.append(fill)
                        if fill.side is Signal.BUY:
                            if not isinstance(fill, SimulatedFill):
                                raise RuntimeError(
                                    "Trailing fill cannot open a position"
                                )
                            if fill.fill_id is None:
                                raise RuntimeError("BUY fill has no identifier")
                            fills_by_id[fill.fill_id] = fill
                            if (
                                self._config.trailing_stop_rate is not None
                                and instrument not in trailing_states
                            ):
                                trailing_states[instrument] = _TrailingStopState(
                                    high_watermark=fill.execution_price,
                                )
                            if self._config.stop_loss_rate is not None:
                                if instrument in protective_states:
                                    raise RuntimeError(
                                        "Protective stop already has entry state"
                                    )
                                protective_states[instrument] = (
                                    _ProtectiveStopState(
                                        entry_execution_price=(
                                            fill.execution_price
                                        ),
                                    )
                                )
                            if self._config.max_holding_candles is not None:
                                if instrument in timed_exit_states:
                                    raise RuntimeError(
                                        "Timed exit already has entry state"
                                    )
                                timed_exit_states[instrument] = _TimedExitState(
                                    entry_execution_open_time=(
                                        fill.execution_open_time
                                    ),
                                )
                        elif state.base_balance == 0:
                            trailing_states.pop(instrument, None)
                            protective_states.pop(instrument, None)
                            timed_exit_states.pop(instrument, None)
                    trades.extend(new_trades)
                    if skip is not None:
                        skipped.append(skip)

            if self._config.max_holding_candles is not None:
                for _, _, instrument, _, _ in prepared:
                    state = self._portfolio_state(accounting, instrument)
                    if state.base_balance == 0:
                        continue
                    timed_state = timed_exit_states.get(instrument)
                    if timed_state is None:
                        raise RuntimeError(
                            "Open position has no timed-exit state"
                        )
                    row = current_rows[instrument]
                    timed_result = self._execute_timed_exit(
                        row,
                        state,
                        timed_state,
                        instrument,
                        accounting,
                        fills_by_id,
                        lifecycle,
                    )
                    if timed_result is None:
                        continue
                    _, timed_fill, timed_trades, timed_intent = timed_result
                    if timed_intent.intent_id in intent_ids:
                        raise RuntimeError("Duplicate timed-exit intent")
                    if timed_intent.source_event_id in source_event_ids:
                        raise RuntimeError("Duplicate timed-exit source event")
                    intent_ids.add(timed_intent.intent_id)
                    source_event_ids.add(timed_intent.source_event_id)
                    intents.append(timed_intent)
                    fills.append(timed_fill)
                    trades.extend(timed_trades)
                    del timed_exit_states[instrument]
                    trailing_states.pop(instrument, None)
                    protective_states.pop(instrument, None)

            if self._config.trailing_stop_rate is not None:
                for _, _, instrument, _, _ in prepared:
                    state = self._portfolio_state(accounting, instrument)
                    if state.base_balance == 0:
                        continue
                    trailing_state = trailing_states.get(instrument)
                    if trailing_state is None:
                        raise RuntimeError(
                            "Open position has no trailing-stop state"
                        )
                    row = current_rows[instrument]
                    stop_result = self._execute_trailing_stop(
                        row,
                        state,
                        trailing_state,
                        instrument,
                        accounting,
                        fills_by_id,
                        lifecycle,
                    )
                    if stop_result is not None:
                        _, stop_fill, stop_trades, stop_intent = stop_result
                        if stop_intent.intent_id in intent_ids:
                            raise RuntimeError("Duplicate trailing-stop intent")
                        intent_ids.add(stop_intent.intent_id)
                        source_event_ids.add(stop_intent.source_event_id)
                        intents.append(stop_intent)
                        fills.append(stop_fill)
                        trades.extend(stop_trades)
                        del trailing_states[instrument]
                        protective_states.pop(instrument, None)
                        timed_exit_states.pop(instrument, None)
                    else:
                        trailing_states[instrument] = _TrailingStopState(
                            high_watermark=max(
                                trailing_state.high_watermark,
                                row.high,
                            ),
                        )

            if self._config.stop_loss_rate is not None:
                for _, _, instrument, _, _ in prepared:
                    state = self._portfolio_state(accounting, instrument)
                    if state.base_balance == 0:
                        continue
                    protective_state = protective_states.get(instrument)
                    if protective_state is None:
                        raise RuntimeError(
                            "Open position has no protective-stop state"
                        )
                    row = current_rows[instrument]
                    stop_result = self._execute_protective_stop(
                        row,
                        state,
                        protective_state,
                        instrument,
                        accounting,
                        fills_by_id,
                        lifecycle,
                    )
                    if stop_result is None:
                        continue
                    _, stop_fill, stop_trades, stop_intent = stop_result
                    if stop_intent.intent_id in intent_ids:
                        raise RuntimeError("Duplicate protective-stop intent")
                    if stop_intent.source_event_id in source_event_ids:
                        raise RuntimeError(
                            "Duplicate protective-stop source event"
                        )
                    intent_ids.add(stop_intent.intent_id)
                    source_event_ids.add(stop_intent.source_event_id)
                    intents.append(stop_intent)
                    fills.append(stop_fill)
                    trades.extend(stop_trades)
                    del protective_states[instrument]
                    trailing_states.pop(instrument, None)
                    timed_exit_states.pop(instrument, None)

            mark_prices = {
                instrument: row.close
                for instrument, row in current_rows.items()
            }
            positions = self._position_values(accounting, mark_prices)
            first_row = current_rows[prepared[0][2]]
            equity_curve.append(PortfolioEquityPoint(
                candle_open_time=first_row.open_time,
                available_time=first_row.available_time,
                quote_balance=accounting.quote_balance,
                positions=positions,
                equity=accounting.equity(mark_prices),
            ))

        for _, _, _, _, decisions in prepared:
            last_decision = decisions[-1]
            if last_decision.signal is not Signal.HOLD:
                skipped.append(SkippedDecision(
                    decision=last_decision,
                    reason="no next candle is available for execution",
                ))

        last_marks = {
            instrument: rows[-1].close
            for _, _, instrument, rows, _ in prepared
        }
        positions = self._position_values(accounting, last_marks)
        final_equity = accounting.equity(last_marks)
        total_pnl = final_equity - self._config.initial_cash
        unrealized_pnl = total_pnl - accounting.realized_pnl
        self._assert_portfolio_invariants(
            accounting,
            fills,
            trades,
            intents,
            equity_curve,
            final_equity,
            total_pnl,
            unrealized_pnl,
            lifecycle,
        )
        return PortfolioBacktestReport(
            quote_asset="USDT",
            interval=interval,
            config=self._config,
            position_sizer_name=self._position_sizer.name,
            final_quote_balance=accounting.quote_balance,
            positions=positions,
            final_equity=final_equity,
            realized_pnl=accounting.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_pnl=total_pnl,
            maximum_equity_drawdown=self._maximum_drawdown(equity_curve),
            fills=tuple(fills),
            completed_trades=tuple(trades),
            skipped_decisions=tuple(skipped),
            order_intents=tuple(intents),
            open_lots=accounting.all_lots(),
            lot_realizations=accounting.realizations,
            position_cycles=accounting.cycles,
            equity_curve=tuple(equity_curve),
            orders=lifecycle.orders,
            execution_fills=lifecycle.fills,
        )

    @staticmethod
    def _portfolio_signal_priority(signal: Signal) -> int:
        """Settle exits before entries sharing the same execution open."""
        return {
            Signal.SELL: 0,
            Signal.HOLD: 1,
            Signal.BUY: 2,
        }[signal]

    def _execute_decision(
        self,
        decision: StrategyDecision,
        execution_row: FeatureRow,
        state: PortfolioState,
        instrument: Instrument,
        accounting: PortfolioAccounting,
        fills_by_id: dict[str, SimulatedFill],
        lifecycle: ExecutionAdapter,
        approved_sizing: (
            BuySizingProposal | SellSizingProposal | SizingRejection | None
        ) = None,
        allow_delayed_execution: bool = False,
        execution_fill_fraction: Decimal = Decimal("1"),
        execution_quantity_step: Decimal | None = None,
        cancel_unfilled_remainder: bool = False,
    ) -> tuple[
        PortfolioState,
        SimulatedFill | None,
        tuple[CompletedTrade, ...],
        SkippedDecision | None,
        OrderIntent | None,
    ]:
        if allow_delayed_execution:
            if execution_row.open_time < decision.signal_available_time:
                raise RuntimeError("Decision cannot execute before availability")
        elif decision.signal_available_time != execution_row.open_time:
            raise RuntimeError("Signal is not aligned with the next candle open")
        if (
            not isinstance(execution_fill_fraction, Decimal)
            or not execution_fill_fraction.is_finite()
            or not Decimal("0") < execution_fill_fraction <= 1
        ):
            raise ValueError("execution_fill_fraction must be in (0, 1]")
        if execution_quantity_step is not None and (
            not isinstance(execution_quantity_step, Decimal)
            or not execution_quantity_step.is_finite()
            or execution_quantity_step <= 0
        ):
            raise ValueError("execution_quantity_step must be positive")
        if type(cancel_unfilled_remainder) is not bool:
            raise ValueError("cancel_unfilled_remainder must be a boolean")

        if decision.signal is Signal.HOLD:
            if approved_sizing is not None:
                raise ValueError("HOLD decision cannot have approved sizing")
            return state, None, (), None, None

        if decision.signal is Signal.BUY:
            if (
                state.base_balance > 0
                and not getattr(self._position_sizer, "allows_scale_in", False)
            ):
                return (
                    state,
                    None,
                    (),
                    SkippedDecision(
                        decision=decision,
                        reason=(
                            "BUY ignored because the portfolio already holds "
                            "the base asset"
                        ),
                    ),
                    None,
                )
            sizing = (
                approved_sizing
                if approved_sizing is not None
                else self._size_decision(
                    decision,
                    execution_row,
                    state,
                    instrument,
                )
            )
            if isinstance(sizing, SizingRejection):
                return self._sizing_rejection(decision, state, sizing)
            if not isinstance(sizing, BuySizingProposal):
                raise ValueError("BUY decision requires a BUY sizing proposal")
            return self._buy(
                decision,
                execution_row,
                state,
                instrument,
                sizing,
                accounting,
                lifecycle,
                execution_fill_fraction=execution_fill_fraction,
                execution_quantity_step=execution_quantity_step,
                cancel_unfilled_remainder=cancel_unfilled_remainder,
            )

        if decision.signal is Signal.SELL:
            if state.base_balance == 0:
                return (
                    state,
                    None,
                    (),
                    SkippedDecision(
                        decision=decision,
                        reason="SELL ignored because the portfolio is flat",
                    ),
                    None,
                )
            sizing = (
                approved_sizing
                if approved_sizing is not None
                else self._size_decision(
                    decision,
                    execution_row,
                    state,
                    instrument,
                )
            )
            if isinstance(sizing, SizingRejection):
                return self._sizing_rejection(decision, state, sizing)
            if not isinstance(sizing, SellSizingProposal):
                raise ValueError("SELL decision requires a SELL sizing proposal")
            return self._sell(
                decision,
                execution_row,
                state,
                instrument,
                sizing,
                accounting,
                fills_by_id,
                lifecycle,
                execution_fill_fraction=execution_fill_fraction,
                execution_quantity_step=execution_quantity_step,
                cancel_unfilled_remainder=cancel_unfilled_remainder,
            )

        raise ValueError(f"Unsupported signal value: {decision.signal!r}")

    def _buy(
        self,
        decision: StrategyDecision,
        row: FeatureRow,
        state: PortfolioState,
        instrument: Instrument,
        sizing: BuySizingProposal,
        accounting: PortfolioAccounting,
        lifecycle: ExecutionAdapter,
        *,
        execution_fill_fraction: Decimal = Decimal("1"),
        execution_quantity_step: Decimal | None = None,
        cancel_unfilled_remainder: bool = False,
    ) -> tuple[
        PortfolioState,
        SimulatedFill,
        tuple[CompletedTrade, ...],
        None,
        OrderIntent,
    ]:
        execution_price = row.open * (
            Decimal("1") + self._config.slippage_rate
        )
        if sizing.quote_budget > state.quote_balance:
            raise ValueError("BUY sizing exceeds available quote balance")
        quote_notional = sizing.quote_budget / (
            Decimal("1") + self._config.fee_rate
        )
        requested_fee = sizing.quote_budget - quote_notional
        raw_requested_quantity = quote_notional / execution_price
        requested_quantity = self._paper_fill_quantity(
            raw_requested_quantity,
            Decimal("1"),
            execution_quantity_step,
        )
        base_quantity = self._paper_fill_quantity(
            requested_quantity,
            execution_fill_fraction,
            execution_quantity_step,
        )
        if (
            execution_quantity_step is None
            and base_quantity == requested_quantity
        ):
            fee = requested_fee
            fill_quote_notional = quote_notional
            reservation_amount = sizing.quote_budget
        else:
            fill_quote_notional = base_quantity * execution_price
            fee = fill_quote_notional * self._config.fee_rate
            reservation_amount = (
                requested_quantity
                * execution_price
                * (Decimal("1") + self._config.fee_rate)
            )
        intent = self._strategy_intent(
            decision,
            row,
            instrument,
            requested_quantity,
        )
        fill_id = f"{intent.intent_id}:fill:1"
        self._submit_filled_order(
            lifecycle=lifecycle,
            intent=intent,
            reservation_amount=reservation_amount,
            available_balance=state.quote_balance,
            fill_id=fill_id,
            execution_price=execution_price,
            fill_quantity=base_quantity,
            quote_notional=fill_quote_notional,
            fee=fee,
            executed_at=row.open_time,
            cancel_unfilled_remainder=cancel_unfilled_remainder,
        )
        accounting.record_buy(
            fill_id=fill_id,
            instrument=instrument,
            quantity=base_quantity,
            execution_price=execution_price,
            quote_notional=fill_quote_notional,
            fee=fee,
            executed_at=row.open_time,
        )
        new_state = self._portfolio_state(accounting, instrument)
        fill = self._fill(
            decision,
            row,
            execution_price,
            base_quantity,
            fill_quote_notional,
            fee,
            new_state,
            intent,
            fill_id,
        )
        self._assert_non_negative(new_state)
        return new_state, fill, (), None, intent

    def _sell(
        self,
        decision: StrategyDecision,
        row: FeatureRow,
        state: PortfolioState,
        instrument: Instrument,
        sizing: SellSizingProposal,
        accounting: PortfolioAccounting,
        fills_by_id: dict[str, SimulatedFill],
        lifecycle: ExecutionAdapter,
        *,
        execution_fill_fraction: Decimal = Decimal("1"),
        execution_quantity_step: Decimal | None = None,
        cancel_unfilled_remainder: bool = False,
    ) -> tuple[
        PortfolioState,
        SimulatedFill,
        tuple[CompletedTrade, ...],
        None,
        OrderIntent,
    ]:
        if sizing.base_quantity > state.base_balance:
            raise ValueError("SELL sizing exceeds the open position")
        if (
            sizing.base_quantity != state.base_balance
            and not getattr(
                self._position_sizer,
                "allows_partial_sell",
                False,
            )
        ):
            raise ValueError("Position sizer does not permit partial SELL")
        intent = self._strategy_intent(
            decision,
            row,
            instrument,
            sizing.base_quantity,
        )
        new_state, fill, trades = self._settle_sell(
            row=row,
            state=state,
            instrument=instrument,
            accounting=accounting,
            fills_by_id=fills_by_id,
            lifecycle=lifecycle,
            base_quantity=sizing.base_quantity,
            market_reference_price=row.open,
            decision=decision,
            intent=intent,
            execution_fill_fraction=execution_fill_fraction,
            execution_quantity_step=execution_quantity_step,
            cancel_unfilled_remainder=cancel_unfilled_remainder,
        )
        if not isinstance(fill, SimulatedFill):
            raise RuntimeError("Strategy SELL produced a non-strategy fill")
        return new_state, fill, trades, None, intent

    def _execute_timed_exit(
        self,
        row: FeatureRow,
        state: PortfolioState,
        timed_state: _TimedExitState,
        instrument: Instrument,
        accounting: PortfolioAccounting,
        fills_by_id: dict[str, SimulatedFill],
        lifecycle: ExecutionAdapter,
    ) -> tuple[
        PortfolioState,
        TimedExitFill,
        tuple[CompletedTrade, ...],
        OrderIntent,
    ] | None:
        maximum = self._config.max_holding_candles
        if maximum is None:
            return None
        interval_milliseconds = row.close_time - row.open_time + 1
        elapsed = row.open_time - timed_state.entry_execution_open_time
        if elapsed < maximum * interval_milliseconds:
            return None

        intent = self._timed_exit_intent(
            instrument,
            row,
            state.base_balance,
            timed_state,
            maximum,
        )
        new_state, fill, trades = self._settle_sell(
            row=row,
            state=state,
            instrument=instrument,
            accounting=accounting,
            fills_by_id=fills_by_id,
            lifecycle=lifecycle,
            base_quantity=state.base_balance,
            market_reference_price=row.open,
            intent=intent,
            timed_state=timed_state,
            maximum_holding_candles=maximum,
        )
        if not isinstance(fill, TimedExitFill):
            raise RuntimeError("Timed exit produced an unexpected fill")
        if fill.intent_id is None:
            raise RuntimeError("Timed exit fill has no order intent")
        return new_state, fill, trades, intent

    def _execute_trailing_stop(
        self,
        row: FeatureRow,
        state: PortfolioState,
        trailing_state: _TrailingStopState,
        instrument: Instrument,
        accounting: PortfolioAccounting,
        fills_by_id: dict[str, SimulatedFill],
        lifecycle: ExecutionAdapter,
    ) -> tuple[
        PortfolioState,
        TrailingStopFill,
        tuple[CompletedTrade, ...],
        OrderIntent,
    ] | None:
        rate = self._config.trailing_stop_rate
        if rate is None:
            return None

        active_stop_price = trailing_state.high_watermark * (
            Decimal("1") - rate
        )
        if row.open <= active_stop_price:
            trigger_kind = TrailingStopTriggerKind.GAP_OPEN
            market_reference_price = row.open
        elif row.low <= active_stop_price:
            trigger_kind = TrailingStopTriggerKind.INTRABAR
            market_reference_price = active_stop_price
        else:
            return None

        intent = self._trailing_intent(
            instrument,
            row,
            state.base_balance,
            trigger_kind,
        )
        new_state, fill, trades = self._settle_sell(
            row=row,
            state=state,
            instrument=instrument,
            accounting=accounting,
            fills_by_id=fills_by_id,
            lifecycle=lifecycle,
            base_quantity=state.base_balance,
            market_reference_price=market_reference_price,
            intent=intent,
            trailing_state=trailing_state,
            active_stop_price=active_stop_price,
            trigger_kind=trigger_kind,
        )
        if not isinstance(fill, TrailingStopFill):
            raise RuntimeError("Trailing stop produced a strategy fill")
        if fill.intent_id is None:
            raise RuntimeError("Trailing stop fill has no order intent")
        return new_state, fill, trades, intent

    def _execute_protective_stop(
        self,
        row: FeatureRow,
        state: PortfolioState,
        protective_state: _ProtectiveStopState,
        instrument: Instrument,
        accounting: PortfolioAccounting,
        fills_by_id: dict[str, SimulatedFill],
        lifecycle: ExecutionAdapter,
    ) -> tuple[
        PortfolioState,
        ProtectiveStopFill,
        tuple[CompletedTrade, ...],
        OrderIntent,
    ] | None:
        rate = self._config.stop_loss_rate
        if rate is None:
            return None
        active_stop_price = protective_state.entry_execution_price * (
            Decimal("1") - rate
        )
        if row.open <= active_stop_price:
            trigger_kind = ProtectiveStopTriggerKind.GAP_OPEN
            market_reference_price = row.open
        elif row.low <= active_stop_price:
            trigger_kind = ProtectiveStopTriggerKind.INTRABAR
            market_reference_price = active_stop_price
        else:
            return None

        intent = self._protective_stop_intent(
            instrument,
            row,
            state.base_balance,
            trigger_kind,
        )
        new_state, fill, trades = self._settle_sell(
            row=row,
            state=state,
            instrument=instrument,
            accounting=accounting,
            fills_by_id=fills_by_id,
            lifecycle=lifecycle,
            base_quantity=state.base_balance,
            market_reference_price=market_reference_price,
            intent=intent,
            protective_state=protective_state,
            active_stop_price=active_stop_price,
            protective_trigger_kind=trigger_kind,
        )
        if not isinstance(fill, ProtectiveStopFill):
            raise RuntimeError("Protective stop produced an unexpected fill")
        if fill.intent_id is None:
            raise RuntimeError("Protective stop fill has no order intent")
        return new_state, fill, trades, intent

    def _settle_sell(
        self,
        *,
        row: FeatureRow,
        state: PortfolioState,
        instrument: Instrument,
        accounting: PortfolioAccounting,
        fills_by_id: dict[str, SimulatedFill],
        lifecycle: ExecutionAdapter,
        base_quantity: Decimal,
        market_reference_price: Decimal,
        decision: StrategyDecision | None = None,
        intent: OrderIntent | None = None,
        trailing_state: _TrailingStopState | None = None,
        active_stop_price: Decimal | None = None,
        trigger_kind: TrailingStopTriggerKind | None = None,
        protective_state: _ProtectiveStopState | None = None,
        protective_trigger_kind: ProtectiveStopTriggerKind | None = None,
        timed_state: _TimedExitState | None = None,
        maximum_holding_candles: int | None = None,
        execution_fill_fraction: Decimal = Decimal("1"),
        execution_quantity_step: Decimal | None = None,
        cancel_unfilled_remainder: bool = False,
    ) -> tuple[PortfolioState, Fill, tuple[CompletedTrade, ...]]:
        """Apply one SELL through the shared FIFO accounting path."""
        if intent is None:
            raise RuntimeError("SELL requires an order intent")
        is_strategy_exit = (
            decision is not None
            and intent.source is IntentSource.STRATEGY
        )
        is_trailing_exit = all(
            value is not None
            for value in (trailing_state, active_stop_price, trigger_kind)
        )
        is_protective_exit = all(
            value is not None
            for value in (
                protective_state,
                active_stop_price,
                protective_trigger_kind,
            )
        )
        is_timed_exit = (
            timed_state is not None
            and maximum_holding_candles is not None
        )
        if sum((
            is_strategy_exit,
            is_trailing_exit,
            is_protective_exit,
            is_timed_exit,
        )) != 1:
            raise RuntimeError("SELL must have exactly one execution source")
        if (
            is_trailing_exit
            and intent.source is not IntentSource.TRAILING_STOP
        ):
            raise RuntimeError("Trailing stop requires a trailing-stop intent")
        if is_protective_exit and intent.source is not IntentSource.RISK:
            raise RuntimeError("Protective stop requires a risk intent")
        if is_timed_exit and intent.source is not IntentSource.RISK:
            raise RuntimeError("Timed exit requires a risk intent")

        execution_price = market_reference_price * (
            Decimal("1") - self._config.slippage_rate
        )
        requested_quantity = base_quantity
        base_quantity = self._paper_fill_quantity(
            requested_quantity,
            execution_fill_fraction,
            execution_quantity_step,
        )
        quote_notional = base_quantity * execution_price
        fee = quote_notional * self._config.fee_rate
        fill_id = f"{intent.intent_id}:fill:1"
        self._submit_filled_order(
            lifecycle=lifecycle,
            intent=intent,
            reservation_amount=requested_quantity,
            available_balance=state.base_balance,
            fill_id=fill_id,
            execution_price=execution_price,
            fill_quantity=base_quantity,
            quote_notional=quote_notional,
            fee=fee,
            executed_at=row.open_time,
            cancel_unfilled_remainder=cancel_unfilled_remainder,
        )
        realizations = accounting.record_sell(
            fill_id=fill_id,
            instrument=instrument,
            quantity=base_quantity,
            execution_price=execution_price,
            gross_proceeds=quote_notional,
            fee=fee,
            executed_at=row.open_time,
        )
        new_state = self._portfolio_state(accounting, instrument)
        if decision is not None:
            fill: Fill = self._fill(
                decision,
                row,
                execution_price,
                base_quantity,
                quote_notional,
                fee,
                new_state,
                intent,
                fill_id,
            )
        elif is_trailing_exit:
            if (
                trailing_state is None
                or active_stop_price is None
                or trigger_kind is None
            ):
                raise RuntimeError("Trailing-stop metadata is incomplete")
            fill = TrailingStopFill(
                trigger_kind=trigger_kind,
                trigger_candle_open_time=row.open_time,
                high_watermark=trailing_state.high_watermark,
                active_stop_price=active_stop_price,
                market_reference_price=market_reference_price,
                execution_price=execution_price,
                base_quantity=base_quantity,
                quote_notional=quote_notional,
                fee=fee,
                quote_balance_after=new_state.quote_balance,
                base_balance_after=new_state.base_balance,
                fill_id=fill_id,
                instrument=instrument,
                intent_id=intent.intent_id,
            )
        elif is_protective_exit:
            if (
                protective_state is None
                or active_stop_price is None
                or protective_trigger_kind is None
            ):
                raise RuntimeError("Protective-stop metadata is incomplete")
            fill = ProtectiveStopFill(
                trigger_kind=protective_trigger_kind,
                trigger_candle_open_time=row.open_time,
                entry_execution_price=(
                    protective_state.entry_execution_price
                ),
                active_stop_price=active_stop_price,
                market_reference_price=market_reference_price,
                execution_price=execution_price,
                base_quantity=base_quantity,
                quote_notional=quote_notional,
                fee=fee,
                quote_balance_after=new_state.quote_balance,
                base_balance_after=new_state.base_balance,
                fill_id=fill_id,
                instrument=instrument,
                intent_id=intent.intent_id,
            )
        else:
            if timed_state is None or maximum_holding_candles is None:
                raise RuntimeError("Timed-exit metadata is incomplete")
            fill = TimedExitFill(
                entry_execution_open_time=(
                    timed_state.entry_execution_open_time
                ),
                trigger_candle_open_time=row.open_time,
                maximum_holding_candles=maximum_holding_candles,
                market_reference_price=market_reference_price,
                execution_price=execution_price,
                base_quantity=base_quantity,
                quote_notional=quote_notional,
                fee=fee,
                quote_balance_after=new_state.quote_balance,
                base_balance_after=new_state.base_balance,
                fill_id=fill_id,
                instrument=instrument,
                intent_id=intent.intent_id,
            )
        trades = self._completed_trades(
            realizations,
            fills_by_id,
            fill,
        )
        self._assert_non_negative(new_state)
        return new_state, fill, trades

    @staticmethod
    def _fill(
        decision: StrategyDecision,
        row: FeatureRow,
        execution_price: Decimal,
        base_quantity: Decimal,
        quote_notional: Decimal,
        fee: Decimal,
        state: PortfolioState,
        intent: OrderIntent,
        fill_id: str,
    ) -> SimulatedFill:
        return SimulatedFill(
            side=decision.signal,
            signal_open_time=decision.candle_open_time,
            signal_available_time=decision.signal_available_time,
            execution_open_time=row.open_time,
            market_open_price=row.open,
            execution_price=execution_price,
            base_quantity=base_quantity,
            quote_notional=quote_notional,
            fee=fee,
            quote_balance_after=state.quote_balance,
            base_balance_after=state.base_balance,
            intent_id=intent.intent_id,
            fill_id=fill_id,
            instrument=intent.instrument,
        )

    def _size_decision(
        self,
        decision: StrategyDecision,
        row: FeatureRow,
        state: PortfolioState,
        instrument: Instrument,
    ) -> BuySizingProposal | SellSizingProposal | SizingRejection:
        price_multiplier = (
            Decimal("1") + self._config.slippage_rate
            if decision.signal is Signal.BUY
            else Decimal("1") - self._config.slippage_rate
        )
        context = SizingContext(
            decision=decision,
            instrument=instrument,
            portfolio=state,
            market_open_price=row.open,
            estimated_execution_price=row.open * price_multiplier,
            observed_at=row.open_time,
            fee_rate=self._config.fee_rate,
            slippage_rate=self._config.slippage_rate,
        )
        result = self._position_sizer.size(context)
        if not isinstance(
            result,
            (BuySizingProposal, SellSizingProposal, SizingRejection),
        ):
            raise TypeError("Position sizer returned an unsupported result")
        return result

    @staticmethod
    def _sizing_rejection(
        decision: StrategyDecision,
        state: PortfolioState,
        rejection: SizingRejection,
    ) -> tuple[
        PortfolioState,
        None,
        tuple[CompletedTrade, ...],
        SkippedDecision,
        None,
    ]:
        return (
            state,
            None,
            (),
            SkippedDecision(
                decision=decision,
                reason=f"Position sizing rejected: {rejection.reason}",
            ),
            None,
        )

    @staticmethod
    def _portfolio_state(
        accounting: PortfolioAccounting,
        instrument: Instrument,
    ) -> PortfolioState:
        return PortfolioState(
            quote_balance=accounting.quote_balance,
            base_balance=accounting.base_balance(instrument),
            entry_cost=accounting.cost_basis(instrument),
            realized_pnl=accounting.realized_pnl,
            fees_paid=accounting.fees_paid,
        )

    @staticmethod
    def _completed_trades(
        realizations: Sequence[LotRealization],
        fills_by_id: dict[str, SimulatedFill],
        exit_fill: Fill,
    ) -> tuple[CompletedTrade, ...]:
        trades: list[CompletedTrade] = []
        for realization in realizations:
            try:
                entry_fill = fills_by_id[realization.entry_fill_id]
            except KeyError as error:
                raise RuntimeError(
                    "FIFO realization has no corresponding BUY fill"
                ) from error
            is_legacy_round_trip = (
                len(realizations) == 1
                and realization.quantity == entry_fill.base_quantity
                and realization.entry_fee == entry_fill.fee
                and realization.exit_fee == exit_fill.fee
            )
            if is_legacy_round_trip:
                trade = CompletedTrade(
                    entry_fill=entry_fill,
                    exit_fill=exit_fill,
                    pnl=realization.realized_pnl,
                )
            else:
                trade = CompletedTrade(
                    entry_fill=entry_fill,
                    exit_fill=exit_fill,
                    pnl=realization.realized_pnl,
                    quantity=realization.quantity,
                    entry_cost=realization.cost_basis,
                    exit_net_proceeds=realization.net_proceeds,
                    entry_fee=realization.entry_fee,
                    exit_fee=realization.exit_fee,
                )
            trades.append(trade)
        return tuple(trades)

    @staticmethod
    def _trailing_fill_id(
        instrument: Instrument,
        row: FeatureRow,
        trigger_kind: TrailingStopTriggerKind | None,
    ) -> str:
        if trigger_kind is None:
            raise RuntimeError("Trailing stop trigger kind is required")
        return ":".join((
            "trailing-stop",
            instrument.exchange,
            instrument.market_type,
            instrument.symbol,
            str(row.open_time),
            trigger_kind.value,
        ))

    @staticmethod
    def _strategy_intent(
        decision: StrategyDecision,
        row: FeatureRow,
        instrument: Instrument,
        quantity: Decimal,
    ) -> OrderIntent:
        try:
            side = OrderSide(decision.signal.value)
        except ValueError as error:
            raise ValueError(
                f"Unsupported signal value: {decision.signal!r}"
            ) from error
        source_event_id = ":".join((
            "strategy",
            decision.strategy_name,
            instrument.exchange,
            instrument.market_type,
            instrument.symbol,
            decision.interval,
            str(decision.candle_open_time),
            side.value,
        ))
        return OrderIntent(
            intent_id=f"{source_event_id}:execute:{row.open_time}",
            source_event_id=source_event_id,
            source=IntentSource.STRATEGY,
            instrument=instrument,
            side=side,
            quantity=quantity,
            created_at=row.open_time,
            earliest_execution_at=row.open_time,
            signal_available_time=decision.signal_available_time,
            source_candle_open_time=decision.candle_open_time,
            explanation=decision.explanation,
        )

    @staticmethod
    def _trailing_intent(
        instrument: Instrument,
        row: FeatureRow,
        quantity: Decimal,
        trigger_kind: TrailingStopTriggerKind,
    ) -> OrderIntent:
        source_event_id = ":".join((
            "trailing-stop",
            instrument.exchange,
            instrument.market_type,
            instrument.symbol,
            str(row.open_time),
            trigger_kind.value,
        ))
        return OrderIntent(
            intent_id=f"{source_event_id}:intent",
            source_event_id=source_event_id,
            source=IntentSource.TRAILING_STOP,
            instrument=instrument,
            side=OrderSide.SELL,
            quantity=quantity,
            created_at=row.open_time,
            earliest_execution_at=row.open_time,
            signal_available_time=None,
            source_candle_open_time=row.open_time,
            explanation=(
                "position-level trailing stop closed the remaining position"
            ),
        )

    @staticmethod
    def _protective_stop_intent(
        instrument: Instrument,
        row: FeatureRow,
        quantity: Decimal,
        trigger_kind: ProtectiveStopTriggerKind,
    ) -> OrderIntent:
        source_event_id = ":".join((
            "protective-stop",
            instrument.exchange,
            instrument.market_type,
            instrument.symbol,
            str(row.open_time),
            trigger_kind.value,
        ))
        return OrderIntent(
            intent_id=f"{source_event_id}:intent",
            source_event_id=source_event_id,
            source=IntentSource.RISK,
            instrument=instrument,
            side=OrderSide.SELL,
            quantity=quantity,
            created_at=row.open_time,
            earliest_execution_at=row.open_time,
            signal_available_time=None,
            source_candle_open_time=row.open_time,
            explanation=(
                "fixed entry-relative protective stop closed the remaining "
                "position"
            ),
        )

    @staticmethod
    def _timed_exit_intent(
        instrument: Instrument,
        row: FeatureRow,
        quantity: Decimal,
        timed_state: _TimedExitState,
        maximum_holding_candles: int,
    ) -> OrderIntent:
        source_event_id = ":".join((
            "timed-exit",
            instrument.exchange,
            instrument.market_type,
            instrument.symbol,
            str(timed_state.entry_execution_open_time),
            str(row.open_time),
        ))
        return OrderIntent(
            intent_id=f"{source_event_id}:intent",
            source_event_id=source_event_id,
            source=IntentSource.RISK,
            instrument=instrument,
            side=OrderSide.SELL,
            quantity=quantity,
            created_at=row.open_time,
            earliest_execution_at=row.open_time,
            signal_available_time=None,
            source_candle_open_time=row.open_time,
            explanation=(
                "maximum holding period closed the remaining position after "
                f"{maximum_holding_candles} candles"
            ),
        )

    @staticmethod
    def _submit_filled_order(
        *,
        lifecycle: ExecutionAdapter,
        intent: OrderIntent,
        reservation_amount: Decimal,
        available_balance: Decimal,
        fill_id: str,
        execution_price: Decimal,
        fill_quantity: Decimal,
        quote_notional: Decimal,
        fee: Decimal,
        executed_at: int,
        cancel_unfilled_remainder: bool = False,
    ) -> ExecutionFill:
        order_id = f"order:{intent.intent_id}"
        lifecycle.submit(
            intent,
            order_id=order_id,
            created_at=executed_at,
            reservation_amount=reservation_amount,
            available_balance=available_balance,
        )
        execution_fill = ExecutionFill(
            fill_id=fill_id,
            order_id=order_id,
            instrument=intent.instrument,
            side=intent.side,
            quantity=fill_quantity,
            price=execution_price,
            quote_notional=quote_notional,
            fee=fee,
            fee_asset=intent.instrument.quote_asset,
            executed_at=executed_at,
        )
        order, applied = lifecycle.apply_fill(execution_fill)
        if not applied:
            raise RuntimeError("Deterministic execution fill was not applied")
        if order.status is OrderStatus.PARTIALLY_FILLED:
            if cancel_unfilled_remainder:
                order = lifecycle.cancel(
                    order.order_id,
                    at=executed_at,
                    reason="configured paper partial-fill remainder cancellation",
                )
            else:
                return execution_fill
        if order.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED):
            raise RuntimeError("Deterministic execution order is not terminal")
        return execution_fill

    @staticmethod
    def _paper_fill_quantity(
        requested: Decimal,
        fraction: Decimal,
        step: Decimal | None,
    ) -> Decimal:
        quantity = requested * fraction
        if step is not None:
            quantity = (
                (quantity / step).to_integral_value(rounding=ROUND_DOWN)
                * step
            )
        if quantity <= 0:
            raise ValueError("Paper fill quantity rounds to zero")
        return quantity

    @staticmethod
    def _mark_equity(row: FeatureRow, state: PortfolioState) -> EquityPoint:
        equity = state.quote_balance + state.base_balance * row.close
        if equity < 0:
            raise RuntimeError("Portfolio equity cannot be negative")
        return EquityPoint(
            candle_open_time=row.open_time,
            available_time=row.available_time,
            close_price=row.close,
            quote_balance=state.quote_balance,
            base_balance=state.base_balance,
            equity=equity,
        )

    @staticmethod
    def _position_values(
        accounting: PortfolioAccounting,
        mark_prices: dict[Instrument, Decimal],
    ) -> tuple[PositionValue, ...]:
        positions: list[PositionValue] = []
        for instrument in sorted(
            mark_prices,
            key=lambda item: (
                item.exchange,
                item.market_type,
                item.symbol,
            ),
        ):
            quantity = accounting.base_balance(instrument)
            if quantity == 0:
                continue
            mark_price = mark_prices[instrument]
            cost_basis = accounting.cost_basis(instrument)
            market_value = quantity * mark_price
            positions.append(PositionValue(
                instrument=instrument,
                quantity=quantity,
                cost_basis=cost_basis,
                mark_price=mark_price,
                market_value=market_value,
                unrealized_pnl=market_value - cost_basis,
            ))
        return tuple(positions)

    def _maximum_drawdown(self, points: Sequence[EquityPoint]) -> Decimal:
        peak = self._config.initial_cash
        maximum_drawdown = Decimal("0")
        for point in points:
            if point.equity > peak:
                peak = point.equity
            drawdown = (point.equity - peak) / peak
            if drawdown < maximum_drawdown:
                maximum_drawdown = drawdown
        return maximum_drawdown

    @staticmethod
    def _assert_non_negative(state: PortfolioState) -> None:
        if state.quote_balance < 0 or state.base_balance < 0:
            raise RuntimeError("Portfolio balances cannot be negative")

    def _assert_accounting_invariants(
        self,
        state: PortfolioState,
        fills: Sequence[Fill],
        trades: Sequence[CompletedTrade],
        equity_curve: Sequence[EquityPoint],
        order_intents: Sequence[OrderIntent],
        accounting: PortfolioAccounting,
        instrument: Instrument,
        lifecycle: ExecutionAdapter,
        final_equity: Decimal,
        total_pnl: Decimal,
        unrealized_pnl: Decimal,
    ) -> None:
        """Fail loudly if deterministic portfolio accounting diverges."""
        self._assert_non_negative(state)
        accounting.assert_invariants()
        if state.quote_balance != accounting.quote_balance:
            raise RuntimeError("Portfolio quote balance diverged from accounting")
        if state.base_balance != accounting.base_balance(instrument):
            raise RuntimeError("Portfolio base balance diverged from FIFO lots")
        if state.entry_cost != accounting.cost_basis(instrument):
            raise RuntimeError("Portfolio cost basis diverged from FIFO lots")
        realized_from_trades = sum(
            (trade.pnl for trade in trades),
            Decimal("0"),
        )
        if state.realized_pnl != realized_from_trades:
            raise RuntimeError(
                "Realized PnL does not equal completed-trade PnL"
            )

        fees_from_fills = sum(
            (fill.fee for fill in fills),
            Decimal("0"),
        )
        if state.fees_paid != fees_from_fills:
            raise RuntimeError("Fees paid do not equal fill fees")
        intent_ids = [intent.intent_id for intent in order_intents]
        source_event_ids = [intent.source_event_id for intent in order_intents]
        if len(intent_ids) != len(set(intent_ids)):
            raise RuntimeError("Order intent identifiers must be unique")
        if len(source_event_ids) != len(set(source_event_ids)):
            raise RuntimeError("Source events cannot be executed twice")
        fill_intent_ids = [
            fill.intent_id
            for fill in fills
        ]
        if any(intent_id is None for intent_id in fill_intent_ids):
            raise RuntimeError("Backtest fill has no order intent")
        if sorted(fill_intent_ids) != sorted(intent_ids):
            raise RuntimeError(
                "Backtest fills and order intents do not match one-to-one"
            )
        self._assert_lifecycle_matches(
            lifecycle,
            fills,
            order_intents,
        )
        if final_equity != equity_curve[-1].equity:
            raise RuntimeError("Final equity does not match the equity curve")
        if total_pnl != final_equity - self._config.initial_cash:
            raise RuntimeError("Total PnL does not match final equity")
        if total_pnl != state.realized_pnl + unrealized_pnl:
            raise RuntimeError("Realized and unrealized PnL do not reconcile")

    def _assert_portfolio_invariants(
        self,
        accounting: PortfolioAccounting,
        fills: Sequence[Fill],
        trades: Sequence[CompletedTrade],
        order_intents: Sequence[OrderIntent],
        equity_curve: Sequence[PortfolioEquityPoint],
        final_equity: Decimal,
        total_pnl: Decimal,
        unrealized_pnl: Decimal,
        lifecycle: ExecutionAdapter,
    ) -> None:
        accounting.assert_invariants()
        if accounting.realized_pnl != sum(
            (trade.pnl for trade in trades), Decimal("0")
        ):
            raise RuntimeError(
                "Portfolio realized PnL does not match completed trades"
            )
        if accounting.fees_paid != sum(
            (fill.fee for fill in fills), Decimal("0")
        ):
            raise RuntimeError("Portfolio fill fees do not reconcile")
        if any(fill.instrument is None for fill in fills):
            raise RuntimeError("Portfolio fill has no instrument identity")
        intent_ids = [intent.intent_id for intent in order_intents]
        source_ids = [intent.source_event_id for intent in order_intents]
        if len(intent_ids) != len(set(intent_ids)):
            raise RuntimeError("Portfolio intent identifiers must be unique")
        if len(source_ids) != len(set(source_ids)):
            raise RuntimeError("Portfolio source events must be unique")
        fill_intent_ids = [fill.intent_id for fill in fills]
        if any(value is None for value in fill_intent_ids):
            raise RuntimeError("Strategy fill has no intent identifier")
        if sorted(fill_intent_ids) != sorted(intent_ids):
            raise RuntimeError("Portfolio fills and intents do not reconcile")
        self._assert_lifecycle_matches(
            lifecycle,
            fills,
            order_intents,
        )
        if not equity_curve or equity_curve[-1].equity != final_equity:
            raise RuntimeError("Portfolio final equity curve point diverged")
        if total_pnl != final_equity - self._config.initial_cash:
            raise RuntimeError("Portfolio total PnL does not match equity")
        if total_pnl != accounting.realized_pnl + unrealized_pnl:
            raise RuntimeError("Portfolio realized/unrealized PnL diverged")

    @staticmethod
    def _assert_lifecycle_matches(
        lifecycle: ExecutionAdapter,
        fills: Sequence[Fill],
        intents: Sequence[OrderIntent],
    ) -> None:
        orders = lifecycle.orders
        execution_fills = lifecycle.fills
        if len(orders) != len(intents) or len(execution_fills) != len(fills):
            raise RuntimeError("Order lifecycle counts do not reconcile")
        if any(order.status is not OrderStatus.FILLED for order in orders):
            raise RuntimeError("Backtest lifecycle contains a non-filled order")
        if {order.intent_id for order in orders} != {
            intent.intent_id for intent in intents
        }:
            raise RuntimeError("Orders and intents do not reconcile")
        if {fill.fill_id for fill in execution_fills} != {
            fill.fill_id for fill in fills
        }:
            raise RuntimeError("Execution and backtest fills do not reconcile")
        if any(
            reservation.remaining_amount != 0
            for reservation in lifecycle.reservations
        ):
            raise RuntimeError("Filled backtest order retains a reservation")


class BacktestExecutionSession:
    """Incremental state around the existing strategy execution kernel.

    The session does not implement pricing, fees, reservations, fills, or FIFO
    accounting. It owns the mutable objects already used by ``BacktestEngine``
    and delegates every accepted strategy decision to ``_execute_decision``.
    """

    def __init__(
        self,
        engine: BacktestEngine,
        instrument: Instrument,
    ) -> None:
        self._engine = engine
        self._instrument = instrument
        self._accounting = PortfolioAccounting(engine._config.initial_cash)
        self._lifecycle = SimulatedExecutionAdapter()
        self._fills_by_id: dict[str, SimulatedFill] = {}
        self._fills: list[SimulatedFill] = []
        self._trades: list[CompletedTrade] = []
        self._skipped: list[SkippedDecision] = []
        self._intents: list[OrderIntent] = []
        self._intent_ids: set[str] = set()
        self._source_event_ids: set[str] = set()
        self._processed_decisions: set[tuple[str, str, str, int, Signal]] = set()
        self._final_report: BacktestReport | None = None

    @property
    def instrument(self) -> Instrument:
        return self._instrument

    @property
    def state(self) -> PortfolioState:
        return self._engine._portfolio_state(
            self._accounting,
            self._instrument,
        )

    @property
    def position_sizer_name(self) -> str:
        return self._engine._position_sizer.name

    @property
    def fills(self) -> tuple[SimulatedFill, ...]:
        return tuple(self._fills)

    @property
    def completed_trades(self) -> tuple[CompletedTrade, ...]:
        return tuple(self._trades)

    @property
    def skipped_decisions(self) -> tuple[SkippedDecision, ...]:
        return tuple(self._skipped)

    @property
    def order_intents(self) -> tuple[OrderIntent, ...]:
        return tuple(self._intents)

    @property
    def orders(self) -> tuple[Order, ...]:
        return self._lifecycle.orders

    @property
    def execution_fills(self) -> tuple[ExecutionFill, ...]:
        return self._lifecycle.fills

    @property
    def reservations(self) -> tuple[BalanceReservation, ...]:
        return self._lifecycle.reservations

    def checkpoint(self) -> BacktestExecutionCheckpoint:
        """Return validated state without finalizing the execution session."""
        self._ensure_active()
        self._accounting.assert_invariants()
        state = self.state
        self._engine._assert_non_negative(state)
        if state.fees_paid != sum(
            (fill.fee for fill in self._fills),
            Decimal("0"),
        ):
            raise RuntimeError("Incremental fill fees do not reconcile")
        if state.realized_pnl != sum(
            (trade.pnl for trade in self._trades),
            Decimal("0"),
        ):
            raise RuntimeError("Incremental realized PnL does not reconcile")
        if len(self._lifecycle.orders) != len(self._intents):
            raise RuntimeError("Incremental orders and intents do not reconcile")
        if len(self._lifecycle.fills) != len(self._fills):
            raise RuntimeError("Incremental execution fills do not reconcile")
        return BacktestExecutionCheckpoint(
            state=state,
            fills=tuple(self._fills),
            completed_trades=tuple(self._trades),
            skipped_decisions=tuple(self._skipped),
            order_intents=tuple(self._intents),
            orders=self._lifecycle.orders,
            execution_fills=self._lifecycle.fills,
            reservations=self._lifecycle.reservations,
            execution_events=self._lifecycle.events,
            open_lots=self._accounting.all_lots(),
            lot_realizations=self._accounting.realizations,
            position_cycles=self._accounting.cycles,
        )

    def prepare_sizing(
        self,
        decision: StrategyDecision,
        execution_row: FeatureRow,
    ) -> BuySizingProposal | SellSizingProposal | SizingRejection | None:
        """Calculate the same sizing value the batch engine would use."""
        self._ensure_active()
        state = self.state
        if decision.signal is Signal.HOLD:
            return None
        if (
            decision.signal is Signal.BUY
            and state.base_balance > 0
            and not getattr(
                self._engine._position_sizer,
                "allows_scale_in",
                False,
            )
        ):
            return None
        if decision.signal is Signal.SELL and state.base_balance == 0:
            return None
        return self._engine._size_decision(
            decision,
            execution_row,
            state,
            self._instrument,
        )

    def execute(
        self,
        decision: StrategyDecision,
        execution_row: FeatureRow,
        *,
        approved_sizing: (
            BuySizingProposal | SellSizingProposal | SizingRejection | None
        ) = None,
        allow_delayed_execution: bool = False,
        execution_fill_fraction: Decimal = Decimal("1"),
        execution_quantity_step: Decimal | None = None,
        cancel_unfilled_remainder: bool = False,
    ) -> StrategyExecutionOutcome:
        """Execute one not-previously-processed decision at its next open."""
        self._ensure_active()
        key = self._decision_key(decision)
        if key in self._processed_decisions:
            raise ValueError("Strategy decision has already been processed")
        state, fill, trades, skipped, intent = self._engine._execute_decision(
            decision,
            execution_row,
            self.state,
            self._instrument,
            self._accounting,
            self._fills_by_id,
            self._lifecycle,
            approved_sizing=approved_sizing,
            allow_delayed_execution=allow_delayed_execution,
            execution_fill_fraction=execution_fill_fraction,
            execution_quantity_step=execution_quantity_step,
            cancel_unfilled_remainder=cancel_unfilled_remainder,
        )
        if intent is not None:
            if intent.intent_id in self._intent_ids:
                raise RuntimeError("Duplicate order intent identifier")
            if intent.source_event_id in self._source_event_ids:
                raise RuntimeError("One source event produced multiple intents")
            self._intent_ids.add(intent.intent_id)
            self._source_event_ids.add(intent.source_event_id)
            self._intents.append(intent)
        if fill is not None:
            if fill.fill_id is None:
                raise RuntimeError("Strategy fill has no fill identifier")
            self._fills.append(fill)
            if fill.side is Signal.BUY:
                self._fills_by_id[fill.fill_id] = fill
        self._trades.extend(trades)
        if skipped is not None:
            self._skipped.append(skipped)
        self._processed_decisions.add(key)
        return StrategyExecutionOutcome(
            state=state,
            fill=fill,
            completed_trades=trades,
            skipped_decision=skipped,
            order_intent=intent,
        )

    def reject(
        self,
        decision: StrategyDecision,
        reason: str,
    ) -> StrategyExecutionOutcome:
        """Record a deterministic pre-intent risk rejection."""
        self._ensure_active()
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Risk rejection reason must be non-empty")
        key = self._decision_key(decision)
        if key in self._processed_decisions:
            raise ValueError("Strategy decision has already been processed")
        skipped = SkippedDecision(
            decision=decision,
            reason=f"Risk check rejected decision: {reason}",
        )
        self._skipped.append(skipped)
        self._processed_decisions.add(key)
        return StrategyExecutionOutcome(
            state=self.state,
            fill=None,
            completed_trades=(),
            skipped_decision=skipped,
            order_intent=None,
        )

    def mark_equity(self, row: FeatureRow) -> EquityPoint:
        self._ensure_active()
        return self._engine._mark_equity(row, self.state)

    def finalize(
        self,
        *,
        strategy_name: str,
        symbol: str,
        interval: str,
        exchange: str,
        market_type: str,
        equity_curve: Sequence[EquityPoint],
        last_close: Decimal,
        last_decision: StrategyDecision,
        add_unexecuted_last_decision: bool = True,
    ) -> BacktestReport:
        """Build the standard report and run the standard engine invariants."""
        if self._final_report is not None:
            return self._final_report
        if (
            add_unexecuted_last_decision
            and last_decision.signal is not Signal.HOLD
        ):
            self._skipped.append(SkippedDecision(
                decision=last_decision,
                reason="no next candle is available for execution",
            ))
        state = self.state
        final_equity = self._accounting.equity({
            self._instrument: last_close,
        })
        total_pnl = final_equity - self._engine._config.initial_cash
        unrealized_pnl = total_pnl - state.realized_pnl
        self._engine._assert_accounting_invariants(
            state,
            self._fills,
            self._trades,
            equity_curve,
            self._intents,
            self._accounting,
            self._instrument,
            self._lifecycle,
            final_equity,
            total_pnl,
            unrealized_pnl,
        )
        self._final_report = BacktestReport(
            strategy_name=strategy_name,
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            interval=interval,
            config=self._engine._config,
            final_state=state,
            final_equity=final_equity,
            realized_pnl=state.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_pnl=total_pnl,
            maximum_equity_drawdown=self._engine._maximum_drawdown(
                equity_curve,
            ),
            fills=tuple(self._fills),
            completed_trades=tuple(self._trades),
            skipped_decisions=tuple(self._skipped),
            equity_curve=tuple(equity_curve),
            position_sizer_name=self.position_sizer_name,
            order_intents=tuple(self._intents),
            open_lots=self._accounting.all_lots(),
            lot_realizations=self._accounting.realizations,
            position_cycles=self._accounting.cycles,
            orders=self._lifecycle.orders,
            execution_fills=self._lifecycle.fills,
        )
        return self._final_report

    def _ensure_active(self) -> None:
        if self._final_report is not None:
            raise RuntimeError("Execution session is already finalized")

    @staticmethod
    def _decision_key(
        decision: StrategyDecision,
    ) -> tuple[str, str, str, int, Signal]:
        return (
            decision.strategy_name,
            decision.symbol,
            decision.interval,
            decision.candle_open_time,
            decision.signal,
        )
