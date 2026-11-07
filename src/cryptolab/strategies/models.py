"""Models for research strategy signals."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType


class Signal(str, Enum):
    """Research signal, not an executable order.

    For spot markets BUY means a suggestion to acquire or increase exposure.
    SELL means a suggestion to reduce or exit existing spot exposure; it does
    not mean opening a short position. HOLD means no action suggestion.
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


DiagnosticValue = Decimal | int | str | bool | None


@dataclass(frozen=True)
class StrategyDecision:
    """Immutable research decision available after a candle has closed."""

    strategy_name: str
    symbol: str
    interval: str
    signal: Signal
    candle_open_time: int
    signal_available_time: int
    reference_price: Decimal
    explanation: str
    diagnostics: Mapping[str, DiagnosticValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.strategy_name or not self.symbol or not self.interval:
            raise ValueError("Strategy and market identifiers must be non-empty")
        if not isinstance(self.signal, Signal):
            raise ValueError("signal must be a Signal value")
        if self.candle_open_time < 0:
            raise ValueError("candle_open_time must be non-negative")
        if self.signal_available_time <= self.candle_open_time:
            raise ValueError(
                "signal_available_time must be after candle_open_time"
            )
        if self.reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if not self.explanation:
            raise ValueError("explanation must be non-empty")

        object.__setattr__(
            self,
            "diagnostics",
            MappingProxyType(dict(self.diagnostics)),
        )

