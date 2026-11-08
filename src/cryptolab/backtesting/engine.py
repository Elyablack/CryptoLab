"""Deterministic long-only Spot backtesting engine."""

from collections.abc import Sequence
from decimal import Decimal

from cryptolab.backtesting.models import (
    BacktestConfig,
    BacktestReport,
    CompletedTrade,
    EquityPoint,
    PortfolioState,
    SimulatedFill,
    SkippedDecision,
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


class BacktestEngine:
    """Evaluate one strategy with one independent virtual Spot portfolio."""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self._config = config or BacktestConfig()

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

        state = PortfolioState(
            quote_balance=self._config.initial_cash,
            base_balance=Decimal("0"),
            entry_cost=Decimal("0"),
            realized_pnl=Decimal("0"),
            fees_paid=Decimal("0"),
        )
        fills: list[SimulatedFill] = []
        trades: list[CompletedTrade] = []
        skipped: list[SkippedDecision] = []
        equity_curve: list[EquityPoint] = []
        open_entry_fill: SimulatedFill | None = None

        for index, row in enumerate(eligible_rows):
            if index > 0:
                decision = decisions[index - 1]
                state, fill, trade, skip = self._execute_decision(
                    decision,
                    row,
                    state,
                    open_entry_fill,
                )
                if fill is not None:
                    fills.append(fill)
                    if fill.side is Signal.BUY:
                        open_entry_fill = fill
                    else:
                        open_entry_fill = None
                if trade is not None:
                    trades.append(trade)
                if skip is not None:
                    skipped.append(skip)

            equity_curve.append(self._mark_equity(row, state))

        last_decision = decisions[-1]
        if last_decision.signal is not Signal.HOLD:
            skipped.append(SkippedDecision(
                decision=last_decision,
                reason="no next candle is available for execution",
            ))

        last_close = eligible_rows[-1].close
        final_equity = state.quote_balance + state.base_balance * last_close
        total_pnl = final_equity - self._config.initial_cash
        unrealized_pnl = (
            total_pnl - state.realized_pnl
            if state.base_balance > 0
            else Decimal("0")
        )
        self._assert_accounting_invariants(
            state,
            fills,
            trades,
            equity_curve,
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
        )

    def _execute_decision(
        self,
        decision: StrategyDecision,
        execution_row: FeatureRow,
        state: PortfolioState,
        open_entry_fill: SimulatedFill | None,
    ) -> tuple[
        PortfolioState,
        SimulatedFill | None,
        CompletedTrade | None,
        SkippedDecision | None,
    ]:
        if decision.signal_available_time != execution_row.open_time:
            raise RuntimeError("Signal is not aligned with the next candle open")

        if decision.signal is Signal.HOLD:
            return state, None, None, None

        if decision.signal is Signal.BUY:
            if state.base_balance > 0:
                return state, None, None, SkippedDecision(
                    decision=decision,
                    reason=(
                        "BUY ignored because the portfolio already holds "
                        "the base asset"
                    ),
                )
            return self._buy(decision, execution_row, state)

        if decision.signal is Signal.SELL:
            if state.base_balance == 0:
                return state, None, None, SkippedDecision(
                    decision=decision,
                    reason="SELL ignored because the portfolio is flat",
                )
            if open_entry_fill is None:
                raise RuntimeError("Open position has no corresponding BUY fill")
            return self._sell(decision, execution_row, state, open_entry_fill)

        raise ValueError(f"Unsupported signal value: {decision.signal!r}")

    def _buy(
        self,
        decision: StrategyDecision,
        row: FeatureRow,
        state: PortfolioState,
    ) -> tuple[PortfolioState, SimulatedFill, None, None]:
        execution_price = row.open * (
            Decimal("1") + self._config.slippage_rate
        )
        quote_notional = state.quote_balance / (
            Decimal("1") + self._config.fee_rate
        )
        fee = state.quote_balance - quote_notional
        base_quantity = quote_notional / execution_price
        new_state = PortfolioState(
            quote_balance=Decimal("0"),
            base_balance=base_quantity,
            entry_cost=state.quote_balance,
            realized_pnl=state.realized_pnl,
            fees_paid=state.fees_paid + fee,
        )
        fill = self._fill(
            decision,
            row,
            execution_price,
            base_quantity,
            quote_notional,
            fee,
            new_state,
        )
        self._assert_non_negative(new_state)
        return new_state, fill, None, None

    def _sell(
        self,
        decision: StrategyDecision,
        row: FeatureRow,
        state: PortfolioState,
        entry_fill: SimulatedFill,
    ) -> tuple[PortfolioState, SimulatedFill, CompletedTrade, None]:
        execution_price = row.open * (
            Decimal("1") - self._config.slippage_rate
        )
        base_quantity = state.base_balance
        quote_notional = base_quantity * execution_price
        fee = quote_notional * self._config.fee_rate
        net_proceeds = quote_notional - fee
        trade_pnl = net_proceeds - state.entry_cost
        quote_balance = state.quote_balance + net_proceeds
        new_state = PortfolioState(
            quote_balance=quote_balance,
            base_balance=Decimal("0"),
            entry_cost=Decimal("0"),
            realized_pnl=quote_balance - self._config.initial_cash,
            fees_paid=state.fees_paid + fee,
        )
        fill = self._fill(
            decision,
            row,
            execution_price,
            base_quantity,
            quote_notional,
            fee,
            new_state,
        )
        trade = CompletedTrade(entry_fill=entry_fill, exit_fill=fill, pnl=trade_pnl)
        self._assert_non_negative(new_state)
        return new_state, fill, trade, None

    @staticmethod
    def _fill(
        decision: StrategyDecision,
        row: FeatureRow,
        execution_price: Decimal,
        base_quantity: Decimal,
        quote_notional: Decimal,
        fee: Decimal,
        state: PortfolioState,
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
        )

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
        fills: Sequence[SimulatedFill],
        trades: Sequence[CompletedTrade],
        equity_curve: Sequence[EquityPoint],
        final_equity: Decimal,
        total_pnl: Decimal,
        unrealized_pnl: Decimal,
    ) -> None:
        """Fail loudly if deterministic portfolio accounting diverges."""
        self._assert_non_negative(state)
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
        if final_equity != equity_curve[-1].equity:
            raise RuntimeError("Final equity does not match the equity curve")
        if total_pnl != final_equity - self._config.initial_cash:
            raise RuntimeError("Total PnL does not match final equity")
        if total_pnl != state.realized_pnl + unrealized_pnl:
            raise RuntimeError("Realized and unrealized PnL do not reconcile")
