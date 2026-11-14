"""Sequential, controlled-clock replay over historical closed candles."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from cryptolab.backtesting.engine import (
    BacktestEngine,
    BacktestExecutionSession,
    StrategyExecutionOutcome,
)
from cryptolab.backtesting.models import BacktestConfig, EquityPoint
from cryptolab.backtesting.sizing import (
    BuySizingProposal,
    LegacyAllInAllOutSizer,
    PositionSizer,
    SellSizingProposal,
    SizingRejection,
)
from cryptolab.execution.models import Instrument
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.intervals import get_interval_milliseconds
from cryptolab.market_data.markets import (
    DEFAULT_EXCHANGE,
    SPOT_MARKET_TYPE,
    validate_market_identity,
)
from cryptolab.market_data.models import Candle
from cryptolab.market_data.validator import (
    validate_candle,
    validate_candle_timing,
)
from cryptolab.replay.models import (
    DeterministicReplayReport,
    ReplayCandleResult,
    ReplayEvent,
    ReplayEventType,
    ReplayMetadataValue,
    ReplayPortfolioSnapshot,
)
from cryptolab.replay.risk import (
    AllowAllReplayRiskPolicy,
    ReplayRiskContext,
    ReplayRiskPolicy,
    RiskCheckResult,
)
from cryptolab.strategies.base import Strategy
from cryptolab.strategies.models import Signal, StrategyDecision
from cryptolab.strategies.runner import StrategyRunner


class DeterministicReplayClock:
    """Monotonic clock advanced only by explicit historical events."""

    def __init__(self) -> None:
        self._now: int | None = None

    @property
    def now(self) -> int | None:
        return self._now

    def advance_to(self, timestamp: int) -> int:
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("Replay timestamp must be a non-negative integer")
        if self._now is not None and timestamp < self._now:
            raise ValueError("Replay clock cannot move backwards")
        self._now = timestamp
        return timestamp


class DeterministicReplayRunner:
    """Factory and convenience API for one-symbol historical replay."""

    def __init__(
        self,
        config: BacktestConfig | None = None,
        *,
        position_sizer: PositionSizer | None = None,
        risk_policy: ReplayRiskPolicy | None = None,
        feature_builder: FeatureBuilder | None = None,
    ) -> None:
        self._config = config or BacktestConfig()
        self._position_sizer = position_sizer or LegacyAllInAllOutSizer()
        self._risk_policy = risk_policy or AllowAllReplayRiskPolicy()
        self._feature_builder = feature_builder or FeatureBuilder()
        if (
            not isinstance(self._risk_policy.name, str)
            or not self._risk_policy.name.strip()
        ):
            raise ValueError("Risk policy name must be non-empty")

    def start(
        self,
        strategy: Strategy,
        symbol: str,
        interval: str,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
    ) -> DeterministicReplaySession:
        return DeterministicReplaySession(
            strategy=strategy,
            symbol=symbol,
            interval=interval,
            config=self._config,
            position_sizer=self._position_sizer,
            risk_policy=self._risk_policy,
            feature_builder=self._feature_builder,
            exchange=exchange,
            market_type=market_type,
        )

    def run(
        self,
        strategy: Strategy,
        symbol: str,
        interval: str,
        candles: Iterable[Candle],
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
    ) -> DeterministicReplayReport:
        session = self.start(
            strategy,
            symbol,
            interval,
            exchange=exchange,
            market_type=market_type,
        )
        for candle in candles:
            session.process_candle(candle)
        return session.finish()


class DeterministicReplaySession:
    """Consume candles one at a time and retain an auditable event stream."""

    def __init__(
        self,
        *,
        strategy: Strategy,
        symbol: str,
        interval: str,
        config: BacktestConfig,
        position_sizer: PositionSizer,
        risk_policy: ReplayRiskPolicy,
        feature_builder: FeatureBuilder,
        exchange: str,
        market_type: str,
    ) -> None:
        validate_market_identity(exchange, market_type)
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            raise ValueError("Replay requires a USDT-quoted symbol")
        get_interval_milliseconds(interval)
        self._strategy = strategy
        self._symbol = symbol
        self._interval = interval
        self._exchange = exchange
        self._market_type = market_type
        self._config = config
        self._risk_policy = risk_policy
        self._feature_builder = feature_builder
        self._strategy_runner = StrategyRunner([strategy])
        self._instrument = Instrument(
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            base_asset=symbol.removesuffix("USDT"),
            quote_asset="USDT",
        )
        self._engine = BacktestEngine(
            config,
            position_sizer=position_sizer,
        )
        self._execution: BacktestExecutionSession = (
            self._engine.start_execution_session(self._instrument)
        )
        self._clock = DeterministicReplayClock()
        self._candles: list[Candle] = []
        self._rows: list[FeatureRow] = []
        self._decisions: list[StrategyDecision] = []
        self._pending_decision: StrategyDecision | None = None
        self._events: list[ReplayEvent] = []
        self._event_ids: set[str] = set()
        self._snapshots: list[ReplayPortfolioSnapshot] = []
        self._equity_curve: list[EquityPoint] = []
        self._processed_candles: dict[str, Candle] = {}
        self._candle_results: dict[str, ReplayCandleResult] = {}
        self._candle_event_order: list[str] = []
        self._final_report: DeterministicReplayReport | None = None

    @property
    def clock(self) -> DeterministicReplayClock:
        return self._clock

    @property
    def decisions(self) -> tuple[StrategyDecision, ...]:
        return tuple(self._decisions)

    @property
    def events(self) -> tuple[ReplayEvent, ...]:
        return tuple(self._events)

    @property
    def snapshots(self) -> tuple[ReplayPortfolioSnapshot, ...]:
        return tuple(self._snapshots)

    def process_candle(
        self,
        candle: Candle,
        *,
        event_id: str | None = None,
    ) -> ReplayCandleResult:
        """Process one candle idempotently by its deterministic event ID."""
        if self._final_report is not None:
            raise RuntimeError("Replay session is already finalized")
        if not isinstance(candle, Candle):
            raise ValueError("candle must be a Candle")
        candle_event_id = event_id or self._candle_event_id(candle)
        if not isinstance(candle_event_id, str) or not candle_event_id.strip():
            raise ValueError("Candle event_id must be non-empty")
        previous = self._processed_candles.get(candle_event_id)
        if previous is not None:
            if previous != candle:
                raise ValueError("Conflicting duplicate candle event")
            return self._candle_results[candle_event_id]

        self._validate_next_candle(candle)
        self._clock.advance_to(candle.open_time)
        self._record(
            event_id=f"{candle_event_id}:open",
            event_type=ReplayEventType.CANDLE_OPEN,
            occurred_at=candle.open_time,
            candle_open_time=candle.open_time,
            metadata={"market_open_price": candle.open},
        )

        executed_decision = self._pending_decision
        outcome: StrategyExecutionOutcome | None = None
        if executed_decision is not None:
            outcome = self._process_pending_decision(
                candle_event_id,
                candle,
                executed_decision,
            )
            self._pending_decision = None
        open_snapshot = self._snapshot(
            phase="after_open_execution",
            observed_at=candle.open_time,
            candle_open_time=candle.open_time,
            mark_price=candle.open,
            event_id=f"{candle_event_id}:open-snapshot",
        )

        available_time = candle.close_time + 1
        self._clock.advance_to(available_time)
        self._candles.append(candle)
        self._record(
            event_id=f"{candle_event_id}:closed",
            event_type=ReplayEventType.CANDLE_CLOSED,
            occurred_at=available_time,
            candle_open_time=candle.open_time,
            metadata={"close_price": candle.close},
        )
        self._rows = self._feature_builder.build(
            self._candles,
            interval=self._interval,
            as_of_time=available_time,
        )
        row = self._rows[-1]
        self._record(
            event_id=f"{candle_event_id}:features",
            event_type=ReplayEventType.FEATURE_UPDATED,
            occurred_at=available_time,
            candle_open_time=candle.open_time,
            metadata={"available_feature_rows": len(self._rows)},
        )
        decision = self._strategy_runner.evaluate_latest(
            self._symbol,
            self._interval,
            self._rows,
            as_of_time=available_time,
        )[0]
        self._decisions.append(decision)
        self._pending_decision = decision
        self._record(
            event_id=f"{candle_event_id}:decision",
            event_type=ReplayEventType.STRATEGY_DECISION,
            occurred_at=decision.signal_available_time,
            candle_open_time=candle.open_time,
            metadata={
                "signal": decision.signal.value,
                "strategy": decision.strategy_name,
            },
        )
        equity_point = self._execution.mark_equity(row)
        self._equity_curve.append(equity_point)
        close_snapshot = self._snapshot(
            phase="candle_close",
            observed_at=available_time,
            candle_open_time=candle.open_time,
            mark_price=candle.close,
            event_id=f"{candle_event_id}:close-snapshot",
        )
        result = ReplayCandleResult(
            candle_event_id=candle_event_id,
            candle=candle,
            executed_decision=executed_decision,
            execution_outcome=outcome,
            new_decision=decision,
            open_snapshot=open_snapshot,
            close_snapshot=close_snapshot,
        )
        self._processed_candles[candle_event_id] = candle
        self._candle_results[candle_event_id] = result
        self._candle_event_order.append(candle_event_id)
        return result

    def finish(self) -> DeterministicReplayReport:
        """Finalize once; an actionable last decision remains unexecuted."""
        if self._final_report is not None:
            return self._final_report
        if not self._candles or not self._decisions:
            raise ValueError("Replay requires at least one candle")
        last_decision = self._decisions[-1]
        if last_decision.signal is not Signal.HOLD:
            self._record(
                event_id=(
                    f"replay:{self._symbol}:{last_decision.candle_open_time}:"
                    "unexecuted"
                ),
                event_type=ReplayEventType.UNEXECUTED_DECISION,
                occurred_at=last_decision.signal_available_time,
                candle_open_time=last_decision.candle_open_time,
                metadata={"signal": last_decision.signal.value},
            )
        backtest = self._execution.finalize(
            strategy_name=self._strategy.name,
            symbol=self._symbol,
            interval=self._interval,
            exchange=self._exchange,
            market_type=self._market_type,
            equity_curve=self._equity_curve,
            last_close=self._candles[-1].close,
            last_decision=last_decision,
        )
        self._final_report = DeterministicReplayReport(
            backtest=backtest,
            decisions=tuple(self._decisions),
            events=tuple(self._events),
            snapshots=tuple(self._snapshots),
            candle_event_ids=tuple(self._candle_event_order),
            risk_policy_name=self._risk_policy.name,
        )
        return self._final_report

    def _process_pending_decision(
        self,
        candle_event_id: str,
        candle: Candle,
        decision: StrategyDecision,
    ) -> StrategyExecutionOutcome:
        if decision.signal_available_time != candle.open_time:
            raise RuntimeError("Pending decision is not aligned to next open")
        execution_row = self._open_only_row(candle)
        sizing = self._execution.prepare_sizing(decision, execution_row)
        if sizing is not None:
            self._record(
                event_id=f"{candle_event_id}:sizing",
                event_type=ReplayEventType.POSITION_SIZED,
                occurred_at=candle.open_time,
                candle_open_time=decision.candle_open_time,
                metadata=self._sizing_metadata(sizing),
            )
        risk_context = ReplayRiskContext(
            decision=decision,
            sizing=sizing,
            portfolio=self._execution.state,
            instrument=self._instrument,
            market_open_price=candle.open,
            observed_at=candle.open_time,
        )
        risk = self._risk_policy.check(risk_context)
        if not isinstance(risk, RiskCheckResult):
            raise TypeError("Risk policy returned an unsupported result")
        self._record(
            event_id=f"{candle_event_id}:risk",
            event_type=ReplayEventType.RISK_CHECKED,
            occurred_at=candle.open_time,
            candle_open_time=decision.candle_open_time,
            metadata={"accepted": risk.accepted, "reason": risk.reason},
        )
        if risk.accepted:
            outcome = self._execution.execute(
                decision,
                execution_row,
                approved_sizing=sizing,
            )
        else:
            outcome = self._execution.reject(decision, risk.reason)
        if outcome.order_intent is not None:
            self._record(
                event_id=f"{candle_event_id}:intent",
                event_type=ReplayEventType.ORDER_INTENT_CREATED,
                occurred_at=candle.open_time,
                candle_open_time=decision.candle_open_time,
                metadata={
                    "intent_id": outcome.order_intent.intent_id,
                    "side": outcome.order_intent.side.value,
                },
            )
        if outcome.fill is not None:
            self._record(
                event_id=f"{candle_event_id}:fill",
                event_type=ReplayEventType.FILL_APPLIED,
                occurred_at=candle.open_time,
                candle_open_time=decision.candle_open_time,
                metadata={
                    "fill_id": outcome.fill.fill_id,
                    "execution_price": outcome.fill.execution_price,
                },
            )
            self._record(
                event_id=f"{candle_event_id}:accounting",
                event_type=ReplayEventType.ACCOUNTING_UPDATED,
                occurred_at=candle.open_time,
                candle_open_time=decision.candle_open_time,
                metadata={
                    "quote_balance": outcome.state.quote_balance,
                    "base_balance": outcome.state.base_balance,
                },
            )
        if outcome.skipped_decision is not None:
            self._record(
                event_id=f"{candle_event_id}:skipped",
                event_type=ReplayEventType.DECISION_SKIPPED,
                occurred_at=candle.open_time,
                candle_open_time=decision.candle_open_time,
                metadata={"reason": outcome.skipped_decision.reason},
            )
        return outcome

    def _snapshot(
        self,
        *,
        phase: str,
        observed_at: int,
        candle_open_time: int,
        mark_price: Decimal,
        event_id: str,
    ) -> ReplayPortfolioSnapshot:
        state = self._execution.state
        equity = state.quote_balance + state.base_balance * mark_price
        unrealized = equity - self._config.initial_cash - state.realized_pnl
        snapshot = ReplayPortfolioSnapshot(
            sequence=len(self._snapshots) + 1,
            phase=phase,
            observed_at=observed_at,
            candle_open_time=candle_open_time,
            mark_price=mark_price,
            state=state,
            equity=equity,
            unrealized_pnl=unrealized,
        )
        self._snapshots.append(snapshot)
        self._record(
            event_id=event_id,
            event_type=ReplayEventType.PORTFOLIO_SNAPSHOT,
            occurred_at=observed_at,
            candle_open_time=candle_open_time,
            metadata={"phase": phase, "equity": equity},
        )
        return snapshot

    def _record(
        self,
        *,
        event_id: str,
        event_type: ReplayEventType,
        occurred_at: int,
        candle_open_time: int,
        metadata: dict[str, ReplayMetadataValue] | None = None,
    ) -> None:
        if event_id in self._event_ids:
            raise ValueError(f"Duplicate replay event: {event_id}")
        self._events.append(ReplayEvent(
            sequence=len(self._events) + 1,
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            source_candle_open_time=candle_open_time,
            metadata={} if metadata is None else metadata,
        ))
        self._event_ids.add(event_id)

    def _validate_next_candle(self, candle: Candle) -> None:
        validate_candle(candle)
        validate_candle_timing(
            candle,
            self._interval,
            as_of_time=candle.close_time + 1,
        )
        if self._candles:
            interval = get_interval_milliseconds(self._interval)
            if candle.open_time - self._candles[-1].open_time != interval:
                raise ValueError("Replay candles must be consecutive")

    def _candle_event_id(self, candle: Candle) -> str:
        return ":".join((
            "closed-candle",
            self._exchange,
            self._market_type,
            self._symbol,
            self._interval,
            str(candle.open_time),
        ))

    @staticmethod
    def _open_only_row(candle: Candle) -> FeatureRow:
        """Project an open event without exposing the candle's future path."""
        return FeatureRow(
            open_time=candle.open_time,
            close_time=candle.close_time,
            available_time=candle.close_time + 1,
            open=candle.open,
            high=candle.open,
            low=candle.open,
            close=candle.open,
            volume=Decimal("0"),
            simple_return=None,
            log_return=None,
            sma20=None,
            sma50=None,
            ema20=None,
            rsi14=None,
            atr14=None,
            rvol20=None,
        )

    @staticmethod
    def _sizing_metadata(
        sizing: BuySizingProposal | SellSizingProposal | SizingRejection,
    ) -> dict[str, ReplayMetadataValue]:
        if isinstance(sizing, BuySizingProposal):
            return {
                "kind": "buy",
                "amount": sizing.quote_budget,
                "reason": sizing.reason,
            }
        if isinstance(sizing, SellSizingProposal):
            return {
                "kind": "sell",
                "amount": sizing.base_quantity,
                "reason": sizing.reason,
            }
        return {
            "kind": "rejected",
            "amount": None,
            "reason": sizing.reason,
        }

