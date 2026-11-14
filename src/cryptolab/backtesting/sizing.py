"""Position-sizing policies for deterministic single-instrument backtests."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from cryptolab.backtesting.models import PortfolioState
from cryptolab.execution.models import Instrument
from cryptolab.strategies.models import Signal, StrategyDecision


LEGACY_SIZER_NAME = "legacy_all_in_all_out"


@dataclass(frozen=True)
class SizingContext:
    """Information available when a decision becomes executable at an open."""

    decision: StrategyDecision
    instrument: Instrument
    portfolio: PortfolioState
    market_open_price: Decimal
    estimated_execution_price: Decimal
    observed_at: int
    fee_rate: Decimal
    slippage_rate: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.decision, StrategyDecision):
            raise ValueError("decision must be a StrategyDecision")
        if not isinstance(self.instrument, Instrument):
            raise ValueError("instrument must be an Instrument")
        if not isinstance(self.portfolio, PortfolioState):
            raise ValueError("portfolio must be a PortfolioState")
        for name, value in (
            ("market_open_price", self.market_open_price),
            ("estimated_execution_price", self.estimated_execution_price),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive Decimal")
        if type(self.observed_at) is not int or self.observed_at < 0:
            raise ValueError("observed_at must be a non-negative integer")
        for name, value in (
            ("fee_rate", self.fee_rate),
            ("slippage_rate", self.slippage_rate),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not Decimal("0") <= value < 1
            ):
                raise ValueError(f"{name} must be between 0 (inclusive) and 1")


@dataclass(frozen=True)
class BuySizingProposal:
    """Quote budget approved for a BUY before final base-quantity conversion."""

    quote_budget: Decimal
    reason: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.quote_budget, Decimal)
            or not self.quote_budget.is_finite()
            or self.quote_budget <= 0
        ):
            raise ValueError("quote_budget must be a finite positive Decimal")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Sizing proposal reason must be non-empty")


@dataclass(frozen=True)
class SellSizingProposal:
    """Base quantity approved for a SELL."""

    base_quantity: Decimal
    reason: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.base_quantity, Decimal)
            or not self.base_quantity.is_finite()
            or self.base_quantity <= 0
        ):
            raise ValueError("base_quantity must be a finite positive Decimal")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Sizing proposal reason must be non-empty")


@dataclass(frozen=True)
class SizingRejection:
    """Auditable reason why an actionable decision produced no intent."""

    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Sizing rejection reason must be non-empty")


SizingResult = BuySizingProposal | SellSizingProposal | SizingRejection


class PositionSizer(Protocol):
    """Calculate allocation without changing strategy or portfolio state."""

    @property
    def name(self) -> str: ...

    def size(self, context: SizingContext) -> SizingResult: ...


@dataclass(frozen=True)
class LegacyAllInAllOutSizer:
    """Preserve the original full-cash BUY and full-position SELL behavior."""

    @property
    def name(self) -> str:
        return LEGACY_SIZER_NAME

    def size(self, context: SizingContext) -> SizingResult:
        if context.decision.signal is Signal.BUY:
            if context.portfolio.quote_balance <= 0:
                return SizingRejection("no quote balance is available for BUY")
            return BuySizingProposal(
                quote_budget=context.portfolio.quote_balance,
                reason="allocate all available quote balance",
            )
        if context.decision.signal is Signal.SELL:
            if context.portfolio.base_balance <= 0:
                return SizingRejection("no base balance is available for SELL")
            return SellSizingProposal(
                base_quantity=context.portfolio.base_balance,
                reason="sell the full open base position",
            )
        return SizingRejection("HOLD does not create an order intent")


@dataclass(frozen=True)
class FixedFractionPositionSizer:
    """Allocate a fixed fraction of quote cash to a single allowed entry.

    Phase 1 deliberately keeps SELL all-out and rejects scale-in in the engine.
    """

    buy_fraction: Decimal

    def __post_init__(self) -> None:
        if (
            not isinstance(self.buy_fraction, Decimal)
            or not self.buy_fraction.is_finite()
            or not Decimal("0") < self.buy_fraction <= 1
        ):
            raise ValueError("buy_fraction must be between 0 and 1 inclusive")

    @property
    def name(self) -> str:
        return f"fixed_fraction(buy_fraction={self.buy_fraction})"

    def size(self, context: SizingContext) -> SizingResult:
        if context.decision.signal is Signal.BUY:
            quote_budget = context.portfolio.quote_balance * self.buy_fraction
            if quote_budget <= 0:
                return SizingRejection("no quote balance is available for BUY")
            return BuySizingProposal(
                quote_budget=quote_budget,
                reason=(
                    "allocate configured fraction "
                    f"{self.buy_fraction} of available quote balance"
                ),
            )
        if context.decision.signal is Signal.SELL:
            if context.portfolio.base_balance <= 0:
                return SizingRejection("no base balance is available for SELL")
            return SellSizingProposal(
                base_quantity=context.portfolio.base_balance,
                reason="sell the full open base position in Phase 1",
            )
        return SizingRejection("HOLD does not create an order intent")


@dataclass(frozen=True)
class FixedFractionScaleSizer:
    """Allocate configurable fractions and permit scale-in/scale-out.

    BUY uses a fraction of currently available quote cash. SELL uses a
    fraction of the currently open base quantity. Strategy signals remain
    quantity-free; this policy alone owns allocation decisions.
    """

    buy_fraction: Decimal
    sell_fraction: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("buy_fraction", self.buy_fraction),
            ("sell_fraction", self.sell_fraction),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not Decimal("0") < value <= 1
            ):
                raise ValueError(f"{name} must be between 0 and 1 inclusive")

    @property
    def name(self) -> str:
        return (
            "fixed_fraction_scale("
            f"buy_fraction={self.buy_fraction},"
            f"sell_fraction={self.sell_fraction})"
        )

    @property
    def allows_scale_in(self) -> bool:
        return True

    @property
    def allows_partial_sell(self) -> bool:
        return self.sell_fraction < 1

    def size(self, context: SizingContext) -> SizingResult:
        if context.decision.signal is Signal.BUY:
            quote_budget = context.portfolio.quote_balance * self.buy_fraction
            if quote_budget <= 0:
                return SizingRejection("no quote balance is available for BUY")
            return BuySizingProposal(
                quote_budget=quote_budget,
                reason=(
                    "scale-in using configured fraction "
                    f"{self.buy_fraction} of available quote balance"
                ),
            )
        if context.decision.signal is Signal.SELL:
            base_quantity = (
                context.portfolio.base_balance * self.sell_fraction
            )
            if base_quantity <= 0:
                return SizingRejection("no base balance is available for SELL")
            return SellSizingProposal(
                base_quantity=base_quantity,
                reason=(
                    "scale-out using configured fraction "
                    f"{self.sell_fraction} of open base quantity"
                ),
            )
        return SizingRejection("HOLD does not create an order intent")
