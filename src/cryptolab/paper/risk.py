"""Persistent pre-intent risk controls for continuous paper trading."""

from collections.abc import Mapping
from decimal import Decimal

from cryptolab.backtesting.sizing import (
    BuySizingProposal,
)
from cryptolab.paper.models import PaperRiskConfig
from cryptolab.replay.risk import (
    ReplayRiskContext,
    ReplayRiskStateValue,
    RiskCheckResult,
)
from cryptolab.strategies.models import Signal


DAY_MS = 86_400_000
ZERO = Decimal("0")


class PaperRiskManager:
    """Stateful fail-closed BUY gate; risk-reducing SELL remains available."""

    name = "paper_risk_v1"

    def __init__(self, config: PaperRiskConfig | None = None) -> None:
        self.config = config or PaperRiskConfig()
        self._paused = False
        self._peak_equity: Decimal | None = None
        self._day_start_equity: Decimal | None = None
        self._day_bucket: int | None = None
        self._last_equity: Decimal | None = None
        self._checks = 0
        self._rejections = 0

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        if type(paused) is not bool:
            raise ValueError("paused must be a boolean")
        self._paused = paused

    def persistence_configuration(self) -> Mapping[str, ReplayRiskStateValue]:
        return {
            "maximum_order_notional": self.config.maximum_order_notional,
            "maximum_position_notional": self.config.maximum_position_notional,
            "maximum_total_exposure": self.config.maximum_total_exposure,
            "minimum_available_cash": self.config.minimum_available_cash,
            "maximum_daily_loss": self.config.maximum_daily_loss,
            "maximum_portfolio_drawdown": (
                self.config.maximum_portfolio_drawdown
            ),
            "data_freshness_limit_ms": self.config.data_freshness_limit_ms,
        }

    def snapshot_state(self) -> Mapping[str, ReplayRiskStateValue]:
        return {
            "peak_equity": self._peak_equity,
            "day_start_equity": self._day_start_equity,
            "day_bucket": self._day_bucket,
            "last_equity": self._last_equity,
            "checks": self._checks,
            "rejections": self._rejections,
        }

    def restore_state(
        self,
        state: Mapping[str, ReplayRiskStateValue],
    ) -> None:
        # Operator controls are restored from paper_controls only after the
        # historical checkpoint has been reconstructed. They must not alter
        # past risk decisions during replay.
        self._paused = False
        expected = {
            "peak_equity",
            "day_start_equity",
            "day_bucket",
            "last_equity",
            "checks",
            "rejections",
        }
        if set(state) != expected:
            raise ValueError("Paper risk state has unexpected fields")
        self._peak_equity = _optional_positive_decimal(
            state["peak_equity"], "peak_equity"
        )
        self._day_start_equity = _optional_positive_decimal(
            state["day_start_equity"], "day_start_equity"
        )
        self._last_equity = _optional_positive_decimal(
            state["last_equity"], "last_equity"
        )
        bucket = state["day_bucket"]
        if bucket is not None and (type(bucket) is not int or bucket < 0):
            raise ValueError("day_bucket must be non-negative or null")
        self._day_bucket = bucket
        self._checks = _non_negative_int(state["checks"], "checks")
        self._rejections = _non_negative_int(
            state["rejections"], "rejections"
        )

    def check(self, context: ReplayRiskContext) -> RiskCheckResult:
        if not isinstance(context, ReplayRiskContext):
            raise ValueError("context must be a ReplayRiskContext")
        equity = context.equity
        if (
            not isinstance(equity, Decimal)
            or not equity.is_finite()
            or equity <= 0
        ):
            return self._reject("portfolio equity is unknown or invalid")
        self._checks += 1
        self._observe_equity(equity, context.observed_at)

        signal = context.decision.signal
        if signal is Signal.HOLD:
            return RiskCheckResult(True, "HOLD creates no order")
        if not context.execution_state_known:
            return self._reject("execution state is not reconciled")
        if signal is Signal.SELL:
            return RiskCheckResult(
                True,
                "risk-reducing SELL is allowed while controls are active",
            )
        if signal is not Signal.BUY:
            return self._reject("unsupported strategy signal")
        if self._paused:
            return self._reject("manual trading pause blocks new BUY orders")
        if context.open_order_count > 0:
            return self._reject("an execution order is already active")
        if context.candle_close_time is None:
            return self._reject("candle close time is unavailable")
        available_at = context.candle_close_time + 1
        age = context.observed_at - available_at
        if age < 0:
            return self._reject("decision data is not available yet")
        if age > self.config.data_freshness_limit_ms:
            return self._reject("market data exceeds the freshness limit")

        sizing = context.sizing
        if not isinstance(sizing, BuySizingProposal):
            return self._reject("BUY has no valid sizing proposal")
        order_notional = sizing.quote_budget
        if order_notional > self.config.maximum_order_notional:
            return self._reject("maximum order notional exceeded")
        position_notional = (
            context.portfolio.base_balance * context.market_open_price
        )
        if (
            position_notional + order_notional
            > self.config.maximum_position_notional
        ):
            return self._reject("maximum position notional exceeded")
        projected_exposure = (
            position_notional + order_notional + context.reserved_quote
        ) / equity
        if projected_exposure > self.config.maximum_total_exposure:
            return self._reject("maximum total exposure exceeded")
        available_cash = (
            context.portfolio.quote_balance
            - context.reserved_quote
            - order_notional
        )
        if available_cash < self.config.minimum_available_cash:
            return self._reject("minimum available cash would be breached")

        if self._day_start_equity is None or self._peak_equity is None:
            raise RuntimeError("Risk equity state was not initialized")
        daily_loss = (equity - self._day_start_equity) / self._day_start_equity
        if daily_loss <= -self.config.maximum_daily_loss:
            return self._reject("maximum daily loss reached")
        drawdown = (equity - self._peak_equity) / self._peak_equity
        if drawdown <= -self.config.maximum_portfolio_drawdown:
            return self._reject("maximum portfolio drawdown reached")
        return RiskCheckResult(True, "paper risk limits accepted BUY")

    def _observe_equity(self, equity: Decimal, observed_at: int) -> None:
        bucket = observed_at // DAY_MS
        if self._day_bucket != bucket:
            self._day_bucket = bucket
            self._day_start_equity = equity
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity
        self._last_equity = equity

    def _reject(self, reason: str) -> RiskCheckResult:
        self._rejections += 1
        return RiskCheckResult(False, reason)


def _optional_positive_decimal(
    value: ReplayRiskStateValue,
    name: str,
) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
        raise ValueError(f"{name} must be a positive Decimal or null")
    return value


def _non_negative_int(value: ReplayRiskStateValue, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value
