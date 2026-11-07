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
        identifiers = (self.strategy_name, self.symbol, self.interval)
        if any(
            not isinstance(value, str) or not value.strip()
            for value in identifiers
        ):
            raise ValueError("Strategy and market identifiers must be non-empty")
        if not isinstance(self.signal, Signal):
            raise ValueError("signal must be a Signal value")
        if type(self.candle_open_time) is not int:
            raise ValueError("candle_open_time must be an integer")
        if type(self.signal_available_time) is not int:
            raise ValueError("signal_available_time must be an integer")
        if self.candle_open_time < 0:
            raise ValueError("candle_open_time must be non-negative")
        if self.signal_available_time <= self.candle_open_time:
            raise ValueError(
                "signal_available_time must be after candle_open_time"
            )
        if (
            not isinstance(self.reference_price, Decimal)
            or not self.reference_price.is_finite()
            or self.reference_price <= 0
        ):
            raise ValueError("reference_price must be a finite positive Decimal")
        if not isinstance(self.explanation, str) or not self.explanation.strip():
            raise ValueError("explanation must be non-empty")

        if not isinstance(self.diagnostics, Mapping):
            raise ValueError("diagnostics must be a mapping")
        diagnostics = dict(self.diagnostics)
        allowed_value_types = (Decimal, int, str, bool, type(None))
        if any(not isinstance(key, str) for key in diagnostics):
            raise ValueError("diagnostic keys must be strings")
        if any(
            not isinstance(value, allowed_value_types)
            for value in diagnostics.values()
        ):
            raise ValueError("diagnostic values must use supported scalar types")

        object.__setattr__(
            self,
            "diagnostics",
            MappingProxyType(diagnostics),
        )

