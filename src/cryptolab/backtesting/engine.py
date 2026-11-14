"""Deterministic long-only Spot backtesting engine."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from cryptolab.backtesting.accounting import (
    LotRealization,
    PortfolioAccounting,
)
from cryptolab.backtesting.models import (
    BacktestConfig,
    BacktestReport,
    CompletedTrade,
    EquityPoint,
    Fill,
    PortfolioState,
    SimulatedFill,
    SkippedDecision,
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
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from cryptolab.execution.adapter import (
    ExecutionAdapter,
    SimulatedExecutionAdapter,
)
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
                    elif state.base_balance == 0:
                        trailing_stop_state = None
                trades.extend(new_trades)
                if skip is not None:
                    skipped.append(skip)

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
                else:
                    trailing_stop_state = _TrailingStopState(
                        high_watermark=max(
                            trailing_stop_state.high_watermark,
                            row.high,
                        ),
                    )

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

        All streams must use the same interval and timestamp grid. Events at
        the same candle open are processed by stable instrument/strategy key,
        so capital allocation is explicit and repeated runs are identical.
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
        equity_curve: list[PortfolioEquityPoint] = []

        for index in range(len(reference_timeline)):
            current_rows = {
                instrument: rows[index]
                for _, _, instrument, rows, _ in prepared
            }
            if index > 0:
                for _, _, instrument, _, decisions in prepared:
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
                        elif state.base_balance == 0:
                            trailing_states.pop(instrument, None)
                    trades.extend(new_trades)
                    if skip is not None:
                        skipped.append(skip)

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
                    else:
                        trailing_states[instrument] = _TrailingStopState(
                            high_watermark=max(
                                trailing_state.high_watermark,
                                row.high,
                            ),
                        )

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

    def _execute_decision(
        self,
        decision: StrategyDecision,
        execution_row: FeatureRow,
        state: PortfolioState,
        instrument: Instrument,
        accounting: PortfolioAccounting,
        fills_by_id: dict[str, SimulatedFill],
        lifecycle: ExecutionAdapter,
    ) -> tuple[
        PortfolioState,
        SimulatedFill | None,
        tuple[CompletedTrade, ...],
        SkippedDecision | None,
        OrderIntent | None,
    ]:
        if decision.signal_available_time != execution_row.open_time:
            raise RuntimeError("Signal is not aligned with the next candle open")

        if decision.signal is Signal.HOLD:
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
            sizing = self._size_decision(
                decision,
                execution_row,
                state,
                instrument,
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
            sizing = self._size_decision(
                decision,
                execution_row,
                state,
                instrument,
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
        fee = sizing.quote_budget - quote_notional
        base_quantity = quote_notional / execution_price
        intent = self._strategy_intent(
            decision,
            row,
            instrument,
            base_quantity,
        )
        fill_id = f"{intent.intent_id}:fill:1"
        self._submit_filled_order(
            lifecycle=lifecycle,
            intent=intent,
            reservation_amount=sizing.quote_budget,
            available_balance=state.quote_balance,
            fill_id=fill_id,
            execution_price=execution_price,
            quote_notional=quote_notional,
            fee=fee,
            executed_at=row.open_time,
        )
        accounting.record_buy(
            fill_id=fill_id,
            instrument=instrument,
            quantity=base_quantity,
            execution_price=execution_price,
            quote_notional=quote_notional,
            fee=fee,
            executed_at=row.open_time,
        )
        new_state = self._portfolio_state(accounting, instrument)
        fill = self._fill(
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
        )
        if not isinstance(fill, SimulatedFill):
            raise RuntimeError("Strategy SELL produced a non-strategy fill")
        return new_state, fill, trades, None, intent

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
    ) -> tuple[PortfolioState, Fill, tuple[CompletedTrade, ...]]:
        """Apply one SELL through the shared FIFO accounting path."""
        if intent is None:
            raise RuntimeError("SELL requires an order intent")
        is_strategy_exit = (
            decision is not None
            and intent.source is IntentSource.STRATEGY
        )
        is_stop_exit = all(
            value is not None
            for value in (trailing_state, active_stop_price, trigger_kind)
        )
        if is_strategy_exit == is_stop_exit:
            raise RuntimeError("SELL must have exactly one execution source")
        if is_stop_exit and intent.source is not IntentSource.TRAILING_STOP:
            raise RuntimeError("Trailing stop requires a trailing-stop intent")

        execution_price = market_reference_price * (
            Decimal("1") - self._config.slippage_rate
        )
        quote_notional = base_quantity * execution_price
        fee = quote_notional * self._config.fee_rate
        fill_id = f"{intent.intent_id}:fill:1"
        self._submit_filled_order(
            lifecycle=lifecycle,
            intent=intent,
            reservation_amount=base_quantity,
            available_balance=state.base_balance,
            fill_id=fill_id,
            execution_price=execution_price,
            quote_notional=quote_notional,
            fee=fee,
            executed_at=row.open_time,
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
        else:
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
    def _submit_filled_order(
        *,
        lifecycle: ExecutionAdapter,
        intent: OrderIntent,
        reservation_amount: Decimal,
        available_balance: Decimal,
        fill_id: str,
        execution_price: Decimal,
        quote_notional: Decimal,
        fee: Decimal,
        executed_at: int,
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
            quantity=intent.quantity,
            price=execution_price,
            quote_notional=quote_notional,
            fee=fee,
            fee_asset=intent.instrument.quote_asset,
            executed_at=executed_at,
        )
        order, applied = lifecycle.apply_fill(execution_fill)
        if not applied or order.status is not OrderStatus.FILLED:
            raise RuntimeError("Deterministic backtest order was not fully filled")
        return execution_fill

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
