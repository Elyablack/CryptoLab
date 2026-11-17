from decimal import Decimal

from cryptolab.features.models import FeatureRow
from cryptolab.research.validation import ResearchPeriod
from cryptolab.strategies import Signal, StrategyDecision
from scripts.diagnose_volatility_compression_breakout import (
    HORIZONS_HOURS,
    calculate_joint_filter_diagnostics,
    summarize_event_horizon,
)


HOUR = 3_600_000


def make_row(
    index: int,
    *,
    open_price: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> FeatureRow:
    open_time = index * HOUR
    return FeatureRow(
        open_time=open_time,
        close_time=open_time + HOUR - 1,
        available_time=open_time + HOUR,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
        simple_return=Decimal("0"),
        log_return=Decimal("0"),
        sma20=Decimal("100"),
        sma50=Decimal("100"),
        ema20=Decimal("100"),
        rsi14=Decimal("50"),
        atr14=Decimal("1"),
        rvol20=Decimal("1"),
    )


def make_decision(
    index: int,
    *,
    atr: bool,
    rvol: bool,
    signal: Signal = Signal.HOLD,
) -> StrategyDecision:
    return StrategyDecision(
        strategy_name="volatility_compression_breakout_v1",
        symbol="BTCUSDT",
        interval="1h",
        signal=signal,
        candle_open_time=index * HOUR,
        signal_available_time=(index + 1) * HOUR,
        reference_price=Decimal("100"),
        explanation="diagnostic fixture",
        diagnostics={
            "new_upper_breakout": True,
            "compression_confirmed": True,
            "bullish_candle_confirmed": True,
            "atr_expansion_confirmed": atr,
            "volume_confirmed": rvol,
        },
    )


def test_event_horizons_are_fixed_before_analysis() -> None:
    assert HORIZONS_HOURS == (6, 12, 24, 48)


def test_event_summary_uses_next_open_and_full_forward_path() -> None:
    rows = [make_row(index) for index in range(10)]
    rows[1] = make_row(1, open_price="100", high="103", low="98", close="101")
    rows[6] = make_row(6, open_price="104", high="107", low="103", close="106")
    period = ResearchPeriod(
        name="development",
        start_index=0,
        end_index=10,
        start_time=0,
        end_time=10 * HOUR,
    )

    summary = summarize_event_horizon(
        rows,
        signal_indices=(0,),
        period=period,
        horizon_hours=6,
        event_group="all_new_upper_breakouts",
    )

    assert summary.observations == 1
    assert summary.mean_return == Decimal("0.06")
    assert summary.mean_mfe == Decimal("0.07")
    assert summary.mean_mae == Decimal("-0.02")
    assert summary.positive_share == Decimal("1")


def test_event_summary_reports_overlap_without_claiming_independence() -> None:
    rows = [make_row(index) for index in range(20)]
    period = ResearchPeriod(
        name="validation",
        start_index=0,
        end_index=20,
        start_time=0,
        end_time=20 * HOUR,
    )

    summary = summarize_event_horizon(
        rows,
        signal_indices=(0, 2, 10),
        period=period,
        horizon_hours=6,
        event_group="compressed_new_upper_breakouts",
    )

    assert summary.observations == 3
    assert summary.overlap_clusters == 2
    assert summary.observations_in_overlapping_clusters == 2
    assert summary.maximum_cluster_size == 2


def test_joint_filter_counts_are_order_independent_and_sequential() -> None:
    decisions = (
        make_decision(0, atr=True, rvol=True, signal=Signal.BUY),
        make_decision(1, atr=True, rvol=False),
        make_decision(2, atr=False, rvol=True),
        make_decision(3, atr=False, rvol=False),
    )

    result = calculate_joint_filter_diagnostics("development", decisions)

    assert result.compressed_breakouts == 4
    assert result.atr_pass == 2
    assert result.rvol_pass == 2
    assert result.both_pass == 1
    assert result.atr_only == 1
    assert result.rvol_only == 1
    assert result.neither_pass == 1
    assert result.independent_atr_failures == 2
    assert result.independent_rvol_failures == 2
    assert result.atr_first_atr_rejections == 2
    assert result.atr_first_rvol_rejections == 1
    assert result.rvol_first_rvol_rejections == 2
    assert result.rvol_first_atr_rejections == 1
