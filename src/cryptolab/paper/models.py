"""Immutable configuration and results for continuous paper trading."""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from cryptolab.market_data.intervals import get_interval_milliseconds


def _positive_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a finite positive Decimal")


def _non_negative_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite non-negative Decimal")


@dataclass(frozen=True)
class PaperRiskConfig:
    """Pre-intent limits for one long-only Spot paper portfolio."""

    maximum_order_notional: Decimal = Decimal("1000")
    maximum_position_notional: Decimal = Decimal("1000")
    maximum_total_exposure: Decimal = Decimal("1")
    minimum_available_cash: Decimal = Decimal("0")
    maximum_daily_loss: Decimal = Decimal("0.05")
    maximum_portfolio_drawdown: Decimal = Decimal("0.20")
    data_freshness_limit_ms: int = 300_000

    def __post_init__(self) -> None:
        _positive_decimal(
            "maximum_order_notional", self.maximum_order_notional
        )
        _positive_decimal(
            "maximum_position_notional", self.maximum_position_notional
        )
        if (
            not isinstance(self.maximum_total_exposure, Decimal)
            or not self.maximum_total_exposure.is_finite()
            or not Decimal("0") < self.maximum_total_exposure <= 1
        ):
            raise ValueError(
                "maximum_total_exposure must be between 0 and 1 inclusive"
            )
        _non_negative_decimal(
            "minimum_available_cash", self.minimum_available_cash
        )
        for name, value in (
            ("maximum_daily_loss", self.maximum_daily_loss),
            (
                "maximum_portfolio_drawdown",
                self.maximum_portfolio_drawdown,
            ),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not Decimal("0") < value < 1
            ):
                raise ValueError(f"{name} must be between 0 and 1")
        if (
            type(self.data_freshness_limit_ms) is not int
            or self.data_freshness_limit_ms <= 0
        ):
            raise ValueError("data_freshness_limit_ms must be positive")


@dataclass(frozen=True)
class PaperExecutionConfig:
    """Deterministic venue assumptions, never claimed as real execution."""

    quantity_step: Decimal = Decimal("0.00000001")
    minimum_quantity: Decimal = Decimal("0.00001")
    minimum_notional: Decimal = Decimal("5")
    simulated_latency_ms: int = 0
    partial_fill_fraction: Decimal = Decimal("1")
    cancel_unfilled_remainder: bool = True

    def __post_init__(self) -> None:
        _positive_decimal("quantity_step", self.quantity_step)
        _positive_decimal("minimum_quantity", self.minimum_quantity)
        _positive_decimal("minimum_notional", self.minimum_notional)
        if (
            type(self.simulated_latency_ms) is not int
            or self.simulated_latency_ms < 0
        ):
            raise ValueError("simulated_latency_ms must be non-negative")
        if (
            not isinstance(self.partial_fill_fraction, Decimal)
            or not self.partial_fill_fraction.is_finite()
            or not Decimal("0") < self.partial_fill_fraction <= 1
        ):
            raise ValueError(
                "partial_fill_fraction must be between 0 and 1 inclusive"
            )
        if type(self.cancel_unfilled_remainder) is not bool:
            raise ValueError("cancel_unfilled_remainder must be a boolean")


@dataclass(frozen=True)
class PaperBotConfig:
    """Identity and scheduling configuration for one Stage 3 process."""

    session_id: str
    market_database: Path
    state_database: Path
    symbol: str = "BTCUSDT"
    interval: str = "1h"
    exchange: str = "bybit"
    market_type: str = "spot"
    warmup_candles: int = 100
    poll_seconds: int = 60

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be non-empty")
        if self.exchange != "bybit" or self.market_type != "spot":
            raise ValueError("Stage 3 supports Bybit Spot only")
        if self.symbol != "BTCUSDT":
            raise ValueError("Stage 3 supports BTCUSDT only")
        get_interval_milliseconds(self.interval)
        if self.interval != "1h":
            raise ValueError("Stage 3 supports the 1h interval only")
        if type(self.warmup_candles) is not int or self.warmup_candles <= 0:
            raise ValueError("warmup_candles must be positive")
        if type(self.poll_seconds) is not int or self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")


@dataclass(frozen=True)
class PaperCycleResult:
    """Compact auditable outcome of one poll cycle."""

    session_id: str
    checked_at: int
    range_start: int | None
    range_end: int | None
    processed_candles: int
    backfilled_candles: int
    live_candles: int
    fills_created: int
    trading_paused: bool
    message: str


@dataclass(frozen=True)
class PaperSessionStatus:
    session_id: str
    checkpoint_sequence: int
    checkpoint_open_time: int | None
    quote_balance: Decimal
    base_balance: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    order_count: int
    fill_count: int
    reservation_count: int
    trading_paused: bool
