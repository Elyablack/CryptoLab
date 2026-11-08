"""Build reproducible baseline and diagnostic strategy research."""

from collections.abc import Sequence
from decimal import Decimal

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    calculate_benchmarks,
)
from cryptolab.backtesting.models import (
    BacktestReport,
    CompletedTrade,
    SimulatedFill,
)
from cryptolab.features.builder import FeatureBuilder
from cryptolab.market_data.markets import DEFAULT_EXCHANGE, SPOT_MARKET_TYPE
from cryptolab.market_data.models import Candle
from cryptolab.research.models import (
    BreakoutDiagnosis,
    ConditionFrequency,
    StrategyResearchReport,
    TrendOpenPositionResearch,
    TrendTradeResearch,
)
from cryptolab.strategies import (
    BreakoutStrategy,
    MeanReversionStrategy,
    Signal,
    StrategyRunner,
    TrendFollowingStrategy,
)
from cryptolab.strategies.base import Strategy
from cryptolab.strategies.models import StrategyDecision


class StrategyResearchRunner:
    """Run unchanged baseline strategies and evidence-oriented diagnostics."""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self._config = config or BacktestConfig()

    def run(
        self,
        symbol: str,
        interval: str,
        candles: Sequence[Candle],
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
        as_of_time: int | None = None,
    ) -> StrategyResearchReport:
        if not candles:
            raise ValueError("At least one candle is required for research")

        effective_as_of = (
            candles[-1].close_time + 1
            if as_of_time is None
            else as_of_time
        )
        rows = FeatureBuilder().build(
            candles,
            interval=interval,
            as_of_time=effective_as_of,
        )
        strategies: tuple[Strategy, ...] = (
            TrendFollowingStrategy(),
            MeanReversionStrategy(),
            BreakoutStrategy(),
        )
        engine = BacktestEngine(self._config)
        reports: list[BacktestReport] = []
        decisions_by_strategy: dict[str, list[StrategyDecision]] = {}
        for strategy in strategies:
            decisions = StrategyRunner([strategy]).run(
                symbol,
                interval,
                rows,
                as_of_time=effective_as_of,
            )
            decisions_by_strategy[strategy.name] = decisions
            reports.append(engine.run(
                strategy,
                symbol,
                interval,
                rows,
                exchange=exchange,
                market_type=market_type,
                as_of_time=effective_as_of,
            ))

        benchmarks = calculate_benchmarks(
            candles,
            symbol,
            interval,
            config=self._config,
            exchange=exchange,
            market_type=market_type,
            as_of_time=effective_as_of,
        )
        trend_report = reports[0]
        trend_decisions = decisions_by_strategy[TrendFollowingStrategy.name]
        trend_trades = self._link_trend_trades(
            trend_report.completed_trades,
            trend_decisions,
        )
        trend_open_position = self._link_open_position(
            trend_report,
            trend_decisions,
        )
        mean_conditions = self._diagnose_mean_reversion(
            decisions_by_strategy[MeanReversionStrategy.name],
        )
        breakout_diagnosis = self._diagnose_breakout(
            decisions_by_strategy[BreakoutStrategy.name],
            reports[2],
        )

        return StrategyResearchReport(
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            interval=interval,
            start_time=candles[0].open_time,
            end_time=candles[-1].close_time + 1,
            candle_count=len(candles),
            strategy_reports=tuple(reports),
            benchmarks=benchmarks,
            trend_trades=trend_trades,
            trend_open_position=trend_open_position,
            mean_reversion_conditions=mean_conditions,
            breakout_diagnosis=breakout_diagnosis,
        )

    @staticmethod
    def _decision_for_fill(
        decisions: Sequence[StrategyDecision],
        fill: SimulatedFill,
    ) -> StrategyDecision:
        for decision in decisions:
            if (
                decision.candle_open_time == fill.signal_open_time
                and decision.signal is fill.side
            ):
                return decision
        raise RuntimeError("Executed fill has no matching strategy decision")

    @classmethod
    def _link_trend_trades(
        cls,
        trades: Sequence[CompletedTrade],
        decisions: Sequence[StrategyDecision],
    ) -> tuple[TrendTradeResearch, ...]:
        linked = []
        for trade in trades:
            linked.append(TrendTradeResearch(
                trade=trade,
                entry_decision=cls._decision_for_fill(
                    decisions,
                    trade.entry_fill,
                ),
                exit_decision=cls._decision_for_fill(
                    decisions,
                    trade.exit_fill,
                ),
                holding_milliseconds=(
                    trade.exit_fill.execution_open_time
                    - trade.entry_fill.execution_open_time
                ),
            ))
        return tuple(linked)

    @classmethod
    def _link_open_position(
        cls,
        report: BacktestReport,
        decisions: Sequence[StrategyDecision],
    ) -> TrendOpenPositionResearch | None:
        if report.final_state.base_balance == 0:
            return None
        entry_fill = next(
            fill for fill in reversed(report.fills)
            if fill.side is Signal.BUY
        )
        return TrendOpenPositionResearch(
            entry_fill=entry_fill,
            entry_decision=cls._decision_for_fill(decisions, entry_fill),
            entry_cost=report.final_state.entry_cost,
            mark_price=report.equity_curve[-1].close_price,
            unrealized_pnl=report.unrealized_pnl,
        )

    @staticmethod
    def _diagnose_mean_reversion(
        decisions: Sequence[StrategyDecision],
    ) -> tuple[ConditionFrequency, ...]:
        evaluated = [
            decision for decision in decisions
            if "previous_rsi14" in decision.diagnostics
        ]
        threshold = MeanReversionStrategy()

        def count(predicate) -> int:
            return sum(predicate(decision) for decision in evaluated)

        previous_rsi_extreme = lambda decision: (
            isinstance(decision.diagnostics.get("previous_rsi14"), Decimal)
            and decision.diagnostics["previous_rsi14"]
            <= threshold.oversold_rsi
        )
        previous_deviation_extreme = lambda decision: (
            isinstance(
                decision.diagnostics.get("previous_sma20_deviation"),
                Decimal,
            )
            and decision.diagnostics["previous_sma20_deviation"]
            <= -threshold.minimum_sma_deviation
        )
        current_rsi_recovered = lambda decision: (
            isinstance(decision.diagnostics.get("rsi14"), Decimal)
            and decision.diagnostics["rsi14"]
            >= threshold.oversold_recovery_rsi
        )
        positive_return = lambda decision: (
            isinstance(decision.diagnostics.get("simple_return"), Decimal)
            and decision.diagnostics["simple_return"] > 0
        )
        has_origin = lambda decision: (
            decision.diagnostics.get("oversold_origin_open_time") is not None
        )
        total = len(evaluated)
        return (
            ConditionFrequency(
                "previous RSI <= 30",
                count(previous_rsi_extreme),
                total,
            ),
            ConditionFrequency(
                "previous price deviation <= -3%",
                count(previous_deviation_extreme),
                total,
            ),
            ConditionFrequency(
                "same prior candle satisfies both extremes",
                count(has_origin),
                total,
            ),
            ConditionFrequency(
                "current RSI >= 35",
                count(current_rsi_recovered),
                total,
            ),
            ConditionFrequency(
                "current simple return > 0",
                count(positive_return),
                total,
            ),
            ConditionFrequency(
                "extreme origin + RSI recovery",
                count(
                    lambda decision: (
                        has_origin(decision)
                        and current_rsi_recovered(decision)
                    )
                ),
                total,
            ),
            ConditionFrequency(
                "extreme origin + RSI recovery + positive return",
                count(
                    lambda decision: (
                        has_origin(decision)
                        and current_rsi_recovered(decision)
                        and positive_return(decision)
                    )
                ),
                total,
            ),
            ConditionFrequency(
                "all BUY conditions",
                sum(
                    decision.signal is Signal.BUY
                    for decision in evaluated
                ),
                total,
            ),
        )

    @staticmethod
    def _diagnose_breakout(
        decisions: Sequence[StrategyDecision],
        report: BacktestReport,
    ) -> BreakoutDiagnosis:
        actionable = [
            decision for decision in decisions
            if decision.signal is not Signal.HOLD
        ]
        return BreakoutDiagnosis(
            buy_signals=sum(
                decision.signal is Signal.BUY
                for decision in actionable
            ),
            sell_signals=sum(
                decision.signal is Signal.SELL
                for decision in actionable
            ),
            skipped_buy_while_holding=sum(
                skipped.decision.signal is Signal.BUY
                for skipped in report.skipped_decisions
            ),
            skipped_sell_while_flat=sum(
                skipped.decision.signal is Signal.SELL
                for skipped in report.skipped_decisions
            ),
            new_events=sum(
                decision.diagnostics.get("event_kind") == "new"
                for decision in actionable
            ),
            continued_events=sum(
                decision.diagnostics.get("event_kind") == "continued"
                for decision in actionable
            ),
        )

