"""Text, JSON, and CSV formatting for signal event studies."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from decimal import Decimal

from cryptolab.research.event_study_models import (
    ReturnDistribution,
    SignalEventStudyReport,
    SignalHorizonSummary,
)
from cryptolab.research.validation import ResearchPeriod


def format_signal_event_study(report: SignalEventStudyReport) -> str:
    """Format signal quality separately from portfolio performance."""
    lines = [
        "CryptoLab Signal Research & Event Study",
        "=======================================",
        (
            f"Dataset: {report.exchange}/{report.market_type} "
            f"{report.symbol} {report.interval}"
        ),
        (
            f"Coverage: {_timestamp(report.start_time)} to "
            f"{_timestamp(report.end_time)} (exclusive), "
            f"candles={report.candle_count}"
        ),
        (
            "Entry model: signal is known after its candle closes; entry is "
            "the next candle open. An Nh return exits at the close of the Nth "
            "hourly candle after entry."
        ),
        (
            "SELL directional return is the negative market return and measures "
            "subsequent downside; it is not a Spot short or portfolio PnL."
        ),
        (
            f"Estimated round-trip cost threshold: "
            f"{report.estimated_round_trip_cost * 100:.4f}% "
            f"(fee={report.fee_rate * 100}% per fill, adverse slippage="
            f"{report.slippage_rate * 100}% per fill)"
        ),
        "Chronological research periods:",
        _period_line(report.split.train),
        _period_line(report.split.validation),
        _period_line(report.split.test),
        "",
        "Signal counts (decisions / next-open eligible)",
    ]
    for count in report.signal_counts:
        lines.append(
            f"  {count.strategy_name}/{count.side.value}/{count.period}: "
            f"{count.decisions}/{count.entry_eligible}"
        )

    lines.extend(["", "Forward directional-return summaries"])
    for summary in report.summaries:
        lines.append(_summary_line(summary))

    lines.extend([
        "",
        "Predeclared regime breakdown at 24h (Validation and studied Test)",
    ])
    selected_regimes = [
        item
        for item in report.regime_summaries
        if item.horizon_hours == 24
        and item.period in {"validation", "studied_test"}
    ]
    if not selected_regimes:
        lines.append("  No 24h regime observations.")
    for item in selected_regimes:
        lines.append(
            f"  {item.strategy_name}/{item.side.value}/{item.period} "
            f"{item.dimension}={item.bucket}: n={item.observations}, "
            f"mean={item.mean_directional_return * 100:.4f}%, "
            f"favorable={item.favorable_share * 100:.2f}%"
        )

    lines.extend(["", "Uncertainty and interpretation"])
    lines.extend(f"  WARNING: {warning}" for warning in report.warnings)
    return "\n".join(lines)


def signal_event_study_to_json(report: SignalEventStudyReport) -> str:
    """Serialize all summaries with exact Decimal strings."""
    payload = {
        "dataset": {
            "exchange": report.exchange,
            "market_type": report.market_type,
            "symbol": report.symbol,
            "interval": report.interval,
            "start_time": report.start_time,
            "end_time": report.end_time,
            "candle_count": report.candle_count,
        },
        "entry_model": {
            "entry": "next_candle_open",
            "exit": "close_of_last_candle_in_forward_horizon",
            "sell_semantics": "downside_directional_event_not_short_position",
        },
        "horizons_hours": list(report.horizons_hours),
        "split": {
            "train": _period_dict(report.split.train),
            "validation": _period_dict(report.split.validation),
            "studied_test": _period_dict(report.split.test),
        },
        "random_seed": report.random_seed,
        "bootstrap_samples": report.bootstrap_samples,
        "minimum_independent_events": report.minimum_independent_events,
        "fee_rate": str(report.fee_rate),
        "slippage_rate": str(report.slippage_rate),
        "estimated_round_trip_cost": str(report.estimated_round_trip_cost),
        "signal_counts": [
            {
                "strategy_name": item.strategy_name,
                "side": item.side.value,
                "period": item.period,
                "decisions": item.decisions,
                "entry_eligible": item.entry_eligible,
            }
            for item in report.signal_counts
        ],
        "observations": [
            {
                "strategy_name": item.strategy_name,
                "side": item.side.value,
                "period": item.period,
                "signal_candle_open_time": item.signal_candle_open_time,
                "signal_available_time": item.signal_available_time,
                "entry_open_time": item.entry_open_time,
                "entry_price": str(item.entry_price),
                "horizon_hours": item.horizon_hours,
                "exit_close_time": item.exit_close_time,
                "exit_price": str(item.exit_price),
                "market_return": str(item.market_return),
                "directional_return": str(item.directional_return),
                "mae": str(item.mae),
                "mfe": str(item.mfe),
                "exceeds_estimated_cost": item.exceeds_estimated_cost,
                "regimes": dict(item.regimes),
            }
            for item in report.observations
        ],
        "summaries": [_summary_dict(item) for item in report.summaries],
        "regime_summaries": [
            {
                "strategy_name": item.strategy_name,
                "side": item.side.value,
                "period": item.period,
                "horizon_hours": item.horizon_hours,
                "dimension": item.dimension,
                "bucket": item.bucket,
                "observations": item.observations,
                "mean_directional_return": str(item.mean_directional_return),
                "favorable_share": str(item.favorable_share),
            }
            for item in report.regime_summaries
        ],
        "warnings": list(report.warnings),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def signal_event_study_to_csv(report: SignalEventStudyReport) -> str:
    """Serialize counts, horizon summaries, and regime rows in one table."""
    fields = [
        "row_type",
        "strategy",
        "side",
        "period",
        "horizon_hours",
        "dimension",
        "bucket",
        "count",
        "entry_eligible",
        "signal_candle_open_time",
        "signal_available_time",
        "entry_open_time",
        "entry_price",
        "exit_close_time",
        "exit_price",
        "mean_market_return",
        "market_return",
        "mean_directional_return",
        "directional_return",
        "median_directional_return",
        "favorable_share",
        "standard_deviation",
        "minimum_directional_return",
        "maximum_directional_return",
        "cost_exceedance_share",
        "mean_mae",
        "mean_mfe",
        "mae",
        "mfe",
        "exceeds_estimated_cost",
        "control_count",
        "control_mean_directional_return",
        "mean_matched_edge",
        "edge_ci_lower",
        "edge_ci_upper",
        "effective_independent_clusters",
        "conclusion",
    ]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for item in report.signal_counts:
        writer.writerow({
            "row_type": "signal_count",
            "strategy": item.strategy_name,
            "side": item.side.value,
            "period": item.period,
            "count": item.decisions,
            "entry_eligible": item.entry_eligible,
        })
    for item in report.observations:
        writer.writerow({
            "row_type": "event_observation",
            "strategy": item.strategy_name,
            "side": item.side.value,
            "period": item.period,
            "horizon_hours": item.horizon_hours,
            "signal_candle_open_time": item.signal_candle_open_time,
            "signal_available_time": item.signal_available_time,
            "entry_open_time": item.entry_open_time,
            "entry_price": item.entry_price,
            "exit_close_time": item.exit_close_time,
            "exit_price": item.exit_price,
            "market_return": item.market_return,
            "directional_return": item.directional_return,
            "mae": item.mae,
            "mfe": item.mfe,
            "exceeds_estimated_cost": str(
                item.exceeds_estimated_cost
            ).lower(),
        })
    for item in report.summaries:
        writer.writerow({
            "row_type": "horizon_summary",
            "strategy": item.strategy_name,
            "side": item.side.value,
            "period": item.period,
            "horizon_hours": item.horizon_hours,
            "count": item.signal_count,
            "mean_market_return": item.mean_market_return,
            "mean_directional_return": item.mean_directional_return,
            "median_directional_return": item.median_directional_return,
            "favorable_share": item.favorable_share,
            "standard_deviation": item.sample_standard_deviation,
            "minimum_directional_return": item.minimum_directional_return,
            "maximum_directional_return": item.maximum_directional_return,
            "cost_exceedance_share": item.cost_exceedance_share,
            "mean_mae": item.mean_mae,
            "mean_mfe": item.mean_mfe,
            "control_count": item.control_count,
            "control_mean_directional_return": (
                item.control_mean_directional_return
            ),
            "mean_matched_edge": item.mean_matched_edge,
            "edge_ci_lower": item.edge_ci_lower,
            "edge_ci_upper": item.edge_ci_upper,
            "effective_independent_clusters": (
                item.effective_independent_clusters
            ),
            "conclusion": item.conclusion,
        })
    for item in report.regime_summaries:
        writer.writerow({
            "row_type": "regime_summary",
            "strategy": item.strategy_name,
            "side": item.side.value,
            "period": item.period,
            "horizon_hours": item.horizon_hours,
            "dimension": item.dimension,
            "bucket": item.bucket,
            "count": item.observations,
            "mean_directional_return": item.mean_directional_return,
            "favorable_share": item.favorable_share,
        })
    return stream.getvalue()


def _summary_line(item: SignalHorizonSummary) -> str:
    return (
        f"  {item.strategy_name}/{item.side.value}/{item.period}/"
        f"{item.horizon_hours}h: n={item.signal_count}, "
        f"mean={_percent(item.mean_directional_return)}, "
        f"median={_percent(item.median_directional_return)}, "
        f"favorable={_percent(item.favorable_share)}, "
        f"sd={_percent(item.sample_standard_deviation)}, "
        f"range=[{_percent(item.minimum_directional_return)}, "
        f"{_percent(item.maximum_directional_return)}], "
        f"cost_exceed={_percent(item.cost_exceedance_share)}, "
        f"MAE={_percent(item.mean_mae)}, MFE={_percent(item.mean_mfe)}, "
        f"control_n={item.control_count}, "
        f"control_mean={_percent(item.control_mean_directional_return)}, "
        f"edge={_percent(item.mean_matched_edge)}, "
        f"edge_95%=[{_percent(item.edge_ci_lower)}, "
        f"{_percent(item.edge_ci_upper)}], "
        f"clusters={item.effective_independent_clusters}, "
        f"result={item.conclusion}"
    )


def _summary_dict(item: SignalHorizonSummary) -> dict[str, object]:
    return {
        "strategy_name": item.strategy_name,
        "side": item.side.value,
        "period": item.period,
        "horizon_hours": item.horizon_hours,
        "signal_count": item.signal_count,
        "mean_market_return": _decimal(item.mean_market_return),
        "mean_directional_return": _decimal(item.mean_directional_return),
        "median_directional_return": _decimal(item.median_directional_return),
        "favorable_share": _decimal(item.favorable_share),
        "sample_standard_deviation": _decimal(item.sample_standard_deviation),
        "distribution": _distribution_dict(item.distribution),
        "minimum_directional_return": _decimal(
            item.minimum_directional_return
        ),
        "maximum_directional_return": _decimal(
            item.maximum_directional_return
        ),
        "cost_exceedance_share": _decimal(item.cost_exceedance_share),
        "mean_mae": _decimal(item.mean_mae),
        "mean_mfe": _decimal(item.mean_mfe),
        "control_count": item.control_count,
        "control_mean_directional_return": _decimal(
            item.control_mean_directional_return
        ),
        "control_median_directional_return": _decimal(
            item.control_median_directional_return
        ),
        "mean_matched_edge": _decimal(item.mean_matched_edge),
        "edge_ci_lower": _decimal(item.edge_ci_lower),
        "edge_ci_upper": _decimal(item.edge_ci_upper),
        "effective_independent_clusters": item.effective_independent_clusters,
        "conclusion": item.conclusion,
    }


def _distribution_dict(item: ReturnDistribution) -> dict[str, str | None]:
    return {
        "p05": _decimal(item.p05),
        "p25": _decimal(item.p25),
        "p50": _decimal(item.p50),
        "p75": _decimal(item.p75),
        "p95": _decimal(item.p95),
    }


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _percent(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value * 100:.4f}%"


def _period_line(period: ResearchPeriod) -> str:
    return (
        f"  {period.name}: {_timestamp(period.start_time)} to "
        f"{_timestamp(period.end_time)} (exclusive), "
        f"candles={period.candle_count}"
    )


def _period_dict(period: ResearchPeriod) -> dict[str, object]:
    return {
        "name": period.name,
        "start_time": period.start_time,
        "end_time": period.end_time,
        "start_index": period.start_index,
        "end_index": period.end_index,
        "candle_count": period.candle_count,
    }


def _timestamp(milliseconds: int) -> str:
    return datetime.fromtimestamp(
        milliseconds / 1000,
        tz=UTC,
    ).isoformat().replace("+00:00", "Z")
