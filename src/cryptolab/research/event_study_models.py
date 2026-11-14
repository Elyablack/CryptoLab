"""Immutable models for directional signal event studies."""

from dataclasses import dataclass
from decimal import Decimal

from cryptolab.research.validation import ChronologicalSplit
from cryptolab.strategies.models import Signal


@dataclass(frozen=True)
class ReturnDistribution:
    """Selected empirical quantiles of directional forward returns."""

    p05: Decimal | None
    p25: Decimal | None
    p50: Decimal | None
    p75: Decimal | None
    p95: Decimal | None


@dataclass(frozen=True)
class SignalCount:
    """Actionable decisions and those with an in-period next-open entry."""

    strategy_name: str
    side: Signal
    period: str
    decisions: int
    entry_eligible: int


@dataclass(frozen=True)
class SignalHorizonSummary:
    """Directional event statistics for one period, side, and horizon."""

    strategy_name: str
    side: Signal
    period: str
    horizon_hours: int
    signal_count: int
    mean_market_return: Decimal | None
    mean_directional_return: Decimal | None
    median_directional_return: Decimal | None
    favorable_share: Decimal | None
    sample_standard_deviation: Decimal | None
    distribution: ReturnDistribution
    minimum_directional_return: Decimal | None
    maximum_directional_return: Decimal | None
    cost_exceedance_share: Decimal | None
    mean_mae: Decimal | None
    mean_mfe: Decimal | None
    control_count: int
    control_mean_directional_return: Decimal | None
    control_median_directional_return: Decimal | None
    mean_matched_edge: Decimal | None
    edge_ci_lower: Decimal | None
    edge_ci_upper: Decimal | None
    effective_independent_clusters: int
    conclusion: str


@dataclass(frozen=True)
class RegimeSummary:
    """One-dimensional, predeclared regime breakdown."""

    strategy_name: str
    side: Signal
    period: str
    horizon_hours: int
    dimension: str
    bucket: str
    observations: int
    mean_directional_return: Decimal
    favorable_share: Decimal


@dataclass(frozen=True)
class SignalEventObservation:
    """Auditable next-open entry and one complete forward outcome."""

    strategy_name: str
    side: Signal
    period: str
    signal_candle_open_time: int
    signal_available_time: int
    entry_open_time: int
    entry_price: Decimal
    horizon_hours: int
    exit_close_time: int
    exit_price: Decimal
    market_return: Decimal
    directional_return: Decimal
    mae: Decimal
    mfe: Decimal
    exceeds_estimated_cost: bool
    regimes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SignalEventStudyReport:
    """Signal quality separated from portfolio sizing and execution PnL."""

    exchange: str
    market_type: str
    symbol: str
    interval: str
    start_time: int
    end_time: int
    candle_count: int
    horizons_hours: tuple[int, ...]
    split: ChronologicalSplit
    random_seed: int
    bootstrap_samples: int
    minimum_independent_events: int
    fee_rate: Decimal
    slippage_rate: Decimal
    estimated_round_trip_cost: Decimal
    signal_counts: tuple[SignalCount, ...]
    observations: tuple[SignalEventObservation, ...]
    summaries: tuple[SignalHorizonSummary, ...]
    regime_summaries: tuple[RegimeSummary, ...]
    warnings: tuple[str, ...]
