"""Deterministic offline venue assumptions for Stage 3 paper trading."""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from cryptolab.backtesting.accounting import PortfolioAccounting
from cryptolab.backtesting.sizing import (
    BuySizingProposal,
    PositionSizer,
    SellSizingProposal,
    SizingContext,
    SizingRejection,
    SizingResult,
)
from cryptolab.execution.adapter import SimulatedExecutionAdapter
from cryptolab.execution.models import (
    Fill,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from cryptolab.paper.models import PaperExecutionConfig


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class PaperPositionSizer:
    """Apply deterministic quantity/minimum rules to an existing sizer."""

    delegate: PositionSizer
    execution: PaperExecutionConfig

    @property
    def name(self) -> str:
        return f"paper_constraints({self.delegate.name})"

    @property
    def allows_scale_in(self) -> bool:
        return bool(getattr(self.delegate, "allows_scale_in", False))

    @property
    def allows_partial_sell(self) -> bool:
        return bool(getattr(self.delegate, "allows_partial_sell", False))

    def size(self, context: SizingContext) -> SizingResult:
        proposed = self.delegate.size(context)
        if isinstance(proposed, SizingRejection):
            return proposed
        if isinstance(proposed, BuySizingProposal):
            gross_notional = proposed.quote_budget / (ONE + context.fee_rate)
            raw_quantity = gross_notional / context.estimated_execution_price
            quantity = _floor_step(raw_quantity, self.execution.quantity_step)
            notional = quantity * context.estimated_execution_price
            fill_quantity = _floor_step(
                quantity * self.execution.partial_fill_fraction,
                self.execution.quantity_step,
            )
            fill_notional = fill_quantity * context.estimated_execution_price
            if fill_quantity < self.execution.minimum_quantity:
                return SizingRejection("BUY quantity is below paper minimum")
            if fill_notional < self.execution.minimum_notional:
                return SizingRejection("BUY notional is below paper minimum")
            total_budget = notional * (ONE + context.fee_rate)
            return BuySizingProposal(
                quote_budget=total_budget,
                reason=f"{proposed.reason}; rounded to paper quantity step",
            )
        if isinstance(proposed, SellSizingProposal):
            quantity = _floor_step(
                proposed.base_quantity,
                self.execution.quantity_step,
            )
            fill_quantity = _floor_step(
                quantity * self.execution.partial_fill_fraction,
                self.execution.quantity_step,
            )
            fill_notional = fill_quantity * context.estimated_execution_price
            if fill_quantity < self.execution.minimum_quantity:
                return SizingRejection("SELL quantity is below paper minimum")
            if fill_notional < self.execution.minimum_notional:
                return SizingRejection("SELL notional is below paper minimum")
            return SellSizingProposal(
                base_quantity=quantity,
                reason=f"{proposed.reason}; rounded to paper quantity step",
            )
        raise TypeError("Position sizer returned an unsupported result")


@dataclass(frozen=True)
class PaperExecutionResult:
    """Audit output from one fake-venue submission."""

    order: Order
    fill: Fill
    fill_applied: bool
    remainder_cancelled: bool


class PaperExecutionSimulator:
    """Simulate deterministic full/partial fills through the shared lifecycle.

    This class never contacts an exchange. A partial fill is an execution
    lifecycle event; it is intentionally distinct from a smaller allocation.
    """

    def __init__(
        self,
        config: PaperExecutionConfig | None = None,
        *,
        adapter: SimulatedExecutionAdapter | None = None,
    ) -> None:
        self.config = config or PaperExecutionConfig()
        self.adapter = adapter or SimulatedExecutionAdapter()

    def submit_and_simulate(
        self,
        intent: OrderIntent,
        *,
        observed_price: Decimal,
        observed_at: int,
        fee_rate: Decimal,
        slippage_rate: Decimal,
        available_balance: Decimal,
    ) -> PaperExecutionResult:
        """Submit one intent and create one deterministic fake venue fill."""
        for name, value in (
            ("observed_price", observed_price),
            ("available_balance", available_balance),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive Decimal")
        for name, value in (
            ("fee_rate", fee_rate),
            ("slippage_rate", slippage_rate),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not ZERO <= value < ONE
            ):
                raise ValueError(f"{name} must be between 0 and 1")
        requested_quantity = _floor_step(
            intent.quantity,
            self.config.quantity_step,
        )
        if requested_quantity != intent.quantity:
            raise ValueError("OrderIntent quantity is not precision-aligned")
        direction = ONE if intent.side is OrderSide.BUY else -ONE
        execution_price = observed_price * (
            ONE + direction * slippage_rate
        )
        requested_notional = requested_quantity * execution_price
        if requested_quantity < self.config.minimum_quantity:
            raise ValueError("Order quantity is below paper minimum")
        if requested_notional < self.config.minimum_notional:
            raise ValueError("Order notional is below paper minimum")
        reservation = (
            requested_notional * (ONE + fee_rate)
            if intent.side is OrderSide.BUY
            else requested_quantity
        )
        order_id = f"paper-order:{intent.intent_id}"
        execution_at = observed_at + self.config.simulated_latency_ms
        order = self.adapter.submit(
            intent,
            order_id=order_id,
            created_at=execution_at,
            reservation_amount=reservation,
            available_balance=available_balance,
        )
        fill_quantity = _floor_step(
            requested_quantity * self.config.partial_fill_fraction,
            self.config.quantity_step,
        )
        if fill_quantity <= 0:
            raise ValueError("Partial-fill quantity rounds to zero")
        quote_notional = fill_quantity * execution_price
        fee = quote_notional * fee_rate
        fill = Fill(
            fill_id=f"paper-fill:{intent.intent_id}:1",
            order_id=order.order_id,
            instrument=intent.instrument,
            side=intent.side,
            quantity=fill_quantity,
            price=execution_price,
            quote_notional=quote_notional,
            fee=fee,
            fee_asset=intent.instrument.quote_asset,
            executed_at=execution_at,
            venue_fill_id=f"fake-venue:{intent.intent_id}:1",
        )
        order, applied = self.adapter.apply_fill(fill)
        cancelled = order.status is OrderStatus.CANCELLED
        if (
            order.remaining_quantity > 0
            and self.config.cancel_unfilled_remainder
            and not order.status.is_terminal
        ):
            order = self.adapter.cancel(
                order.order_id,
                at=execution_at,
                reason="configured paper partial-fill remainder cancellation",
            )
            cancelled = True
        return PaperExecutionResult(
            order=order,
            fill=fill,
            fill_applied=applied,
            remainder_cancelled=cancelled,
        )

    def submit_rejected(
        self,
        intent: OrderIntent,
        *,
        reservation_amount: Decimal,
        available_balance: Decimal,
        rejected_at: int,
        reason: str,
    ) -> Order:
        """Record a deterministic fake-venue rejection and release funds."""
        order = self.adapter.submit(
            intent,
            order_id=f"paper-order:{intent.intent_id}",
            created_at=rejected_at,
            reservation_amount=reservation_amount,
            available_balance=available_balance,
        )
        if order.status is OrderStatus.REJECTED:
            if order.terminal_reason != reason:
                raise ValueError("Conflicting duplicate paper rejection")
            return order
        if order.status.is_terminal:
            raise ValueError("Paper order is already terminal")
        return self.adapter.reject(
            order.order_id,
            at=rejected_at,
            reason=reason,
        )

    @staticmethod
    def apply_fill_to_accounting(
        accounting: PortfolioAccounting,
        fill: Fill,
    ) -> None:
        """Settle a confirmed simulated fill through shared FIFO accounting."""
        if fill.side is OrderSide.BUY:
            accounting.record_buy(
                fill_id=fill.fill_id,
                instrument=fill.instrument,
                quantity=fill.quantity,
                execution_price=fill.price,
                quote_notional=fill.quote_notional,
                fee=fill.fee,
                executed_at=fill.executed_at,
            )
        else:
            accounting.record_sell(
                fill_id=fill.fill_id,
                instrument=fill.instrument,
                quantity=fill.quantity,
                execution_price=fill.price,
                gross_proceeds=fill.quote_notional,
                fee=fill.fee,
                executed_at=fill.executed_at,
            )


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step
