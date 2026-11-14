"""Human-readable and machine-readable optimization reports."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from decimal import Decimal

from cryptolab.research.models import (
    OptimizationMetrics,
    StrategyOptimizationReport,
)
from cryptolab.research.parameter_grid import ParameterSet
from cryptolab.research.validation import ResearchPeriod


def format_optimization_report(report: StrategyOptimizationReport) -> str:
    """Format selection evidence and untouched holdout results."""
    lines = [
        "CryptoLab Strategy Optimization Research",
        "========================================",
        (
            f"Dataset: {report.exchange}/{report.market_type} "
            f"{report.symbol} {report.interval}"
        ),
        (
            f"Coverage: {_timestamp(report.start_time)} to "
            f"{_timestamp(report.end_time)} (exclusive), "
            f"candles={report.candle_count}"
        ),
        "Chronological split (each period starts with a fresh portfolio):",
        _period_line(report.split.train),
        _period_line(report.split.validation),
        _period_line(report.split.test),
        (
            "Boundary policy: earlier candles are indicator warm-up only; "
            "positions are not carried or force-closed across periods."
        ),
        (
            "Selection rule: validation return + 0.5 * validation drawdown "
            "- 0.25 * |train return - validation return|; eligibility requires "
            f">={report.minimum_completed_trades} completed trades in both "
            "train and validation."
        ),
    ]
    for result in report.strategies:
        lines.extend(["", f"Strategy: {result.strategy_name}"])
        for evaluation in result.evaluations:
            lines.append(
                f"  rank={evaluation.validation_rank:>2} "
                f"eligible={'yes' if evaluation.eligible else 'no ':3} "
                f"score={evaluation.selection_score:.8f} "
                f"train_return={evaluation.train.total_return * 100:.4f}% "
                f"train_trades={evaluation.train.completed_trades} "
                f"validation_return="
                f"{evaluation.validation.total_return * 100:.4f}% "
                f"validation_dd="
                f"{evaluation.validation.maximum_drawdown * 100:.4f}% "
                f"validation_trades={evaluation.validation.completed_trades} "
                f"params={_parameters(evaluation.parameters)}"
            )
        lines.append(
            "  Baseline holdout: "
            + _metrics_line(result.baseline_test)
            + f" params={_parameters(result.baseline_parameters)}"
        )
        if result.selected_parameters is None:
            lines.append("  Selected configuration: none (inconclusive)")
        else:
            lines.append(
                "  Selected configuration (chosen without holdout): "
                f"{_parameters(result.selected_parameters)}"
            )
            lines.append(
                "  Selected holdout: " + _metrics_line(result.selected_test)
            )
        for fold in result.walk_forward:
            lines.append(
                f"  {fold.validation_period.name}: "
                f"train_return={fold.train.total_return * 100:.4f}%, "
                f"validation_return={fold.validation.total_return * 100:.4f}%, "
                f"validation_trades={fold.validation.completed_trades}"
            )
        for warning in result.warnings:
            lines.append(f"  WARNING: {warning}")

    lines.extend(["", "Benchmarks"])
    for benchmark in report.benchmarks:
        lines.append(
            f"  {benchmark.period.name}/{benchmark.name}: "
            f"{_metrics_line(benchmark.metrics)}"
        )
    lines.extend([
        "",
        (
            "Interpretation: the holdout is reported once after selection. "
            "A positive or better result is evidence for further research, "
            "not proof of future profitability."
        ),
    ])
    return "\n".join(lines)


def optimization_report_to_json(report: StrategyOptimizationReport) -> str:
    """Serialize report values losslessly as JSON strings for Decimals."""
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
        "split": {
            name: _period_dict(period)
            for name, period in (
                ("train", report.split.train),
                ("validation", report.split.validation),
                ("test", report.split.test),
            )
        },
        "minimum_completed_trades": report.minimum_completed_trades,
        "execution": {
            "initial_cash": str(report.config.initial_cash),
            "fee_rate": str(report.config.fee_rate),
            "slippage_rate": str(report.config.slippage_rate),
        },
        "strategies": [
            {
                "strategy_name": result.strategy_name,
                "evaluations": [
                    {
                        "validation_rank": item.validation_rank,
                        "eligible": item.eligible,
                        "selection_score": str(item.selection_score),
                        "parameters": _parameter_dict(item.parameters),
                        "train": _metrics_dict(item.train),
                        "validation": _metrics_dict(item.validation),
                    }
                    for item in result.evaluations
                ],
                "baseline_parameters": _parameter_dict(
                    result.baseline_parameters
                ),
                "baseline_test": _metrics_dict(result.baseline_test),
                "selected_parameters": (
                    None
                    if result.selected_parameters is None
                    else _parameter_dict(result.selected_parameters)
                ),
                "selected_test": (
                    None
                    if result.selected_test is None
                    else _metrics_dict(result.selected_test)
                ),
                "walk_forward": [
                    {
                        "train_period": _period_dict(item.train_period),
                        "validation_period": _period_dict(
                            item.validation_period
                        ),
                        "train": _metrics_dict(item.train),
                        "validation": _metrics_dict(item.validation),
                    }
                    for item in result.walk_forward
                ],
                "warnings": list(result.warnings),
            }
            for result in report.strategies
        ],
        "benchmarks": [
            {
                "period": item.period.name,
                "name": item.name,
                "metrics": _metrics_dict(item.metrics),
            }
            for item in report.benchmarks
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def optimization_report_to_csv(report: StrategyOptimizationReport) -> str:
    """Serialize one row per grid configuration for tabular analysis."""
    stream = io.StringIO()
    fieldnames = [
        "strategy",
        "rank",
        "eligible",
        "selection_score",
        "parameters",
        "train_return",
        "train_drawdown",
        "train_trades",
        "train_profit_factor",
        "validation_return",
        "validation_drawdown",
        "validation_trades",
        "validation_profit_factor",
    ]
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for result in report.strategies:
        for item in result.evaluations:
            writer.writerow({
                "strategy": result.strategy_name,
                "rank": item.validation_rank,
                "eligible": str(item.eligible).lower(),
                "selection_score": item.selection_score,
                "parameters": _parameters(item.parameters),
                "train_return": item.train.total_return,
                "train_drawdown": item.train.maximum_drawdown,
                "train_trades": item.train.completed_trades,
                "train_profit_factor": item.train.profit_factor,
                "validation_return": item.validation.total_return,
                "validation_drawdown": item.validation.maximum_drawdown,
                "validation_trades": item.validation.completed_trades,
                "validation_profit_factor": item.validation.profit_factor,
            })
    return stream.getvalue()


def _metrics_line(metrics: OptimizationMetrics | None) -> str:
    if metrics is None:
        return "not evaluated"
    win_rate = "N/A" if metrics.win_rate is None else f"{metrics.win_rate * 100:.2f}%"
    profit_factor = (
        "N/A" if metrics.profit_factor is None else f"{metrics.profit_factor:.4f}"
    )
    return (
        f"return={metrics.total_return * 100:.4f}%, "
        f"final={metrics.final_equity:.8f}, "
        f"pnl={metrics.total_pnl:.8f}, "
        f"max_dd={metrics.maximum_drawdown * 100:.4f}%, "
        f"trades={metrics.completed_trades}, win_rate={win_rate}, "
        f"profit_factor={profit_factor}, fees={metrics.fees_paid:.8f}, "
        f"exposure={metrics.exposure_percentage * 100:.2f}%, "
        f"open={'yes' if metrics.has_open_position else 'no'}"
    )


def _metrics_dict(metrics: OptimizationMetrics) -> dict[str, object]:
    return {
        "total_return": str(metrics.total_return),
        "final_equity": str(metrics.final_equity),
        "total_pnl": str(metrics.total_pnl),
        "realized_pnl": str(metrics.realized_pnl),
        "unrealized_pnl": str(metrics.unrealized_pnl),
        "maximum_drawdown": str(metrics.maximum_drawdown),
        "fills": metrics.fills,
        "completed_trades": metrics.completed_trades,
        "win_rate": None if metrics.win_rate is None else str(metrics.win_rate),
        "profit_factor": (
            None if metrics.profit_factor is None else str(metrics.profit_factor)
        ),
        "fees_paid": str(metrics.fees_paid),
        "exposure_percentage": str(metrics.exposure_percentage),
        "skipped_buys": metrics.skipped_buys,
        "skipped_sells": metrics.skipped_sells,
        "has_open_position": metrics.has_open_position,
    }


def _period_line(period: ResearchPeriod) -> str:
    return (
        f"  {period.name}: {_timestamp(period.start_time)} to "
        f"{_timestamp(period.end_time)} (exclusive), "
        f"candles={period.candle_count}"
    )


def _period_dict(period: ResearchPeriod) -> dict[str, object]:
    return {
        "name": period.name,
        "start_index": period.start_index,
        "end_index": period.end_index,
        "start_time": period.start_time,
        "end_time": period.end_time,
        "candle_count": period.candle_count,
    }


def _parameter_dict(parameters: ParameterSet) -> dict[str, object]:
    return {
        name: str(value) if isinstance(value, Decimal) else value
        for name, value in parameters.values
    }


def _parameters(parameters: ParameterSet) -> str:
    return ",".join(f"{name}={value}" for name, value in parameters.values)


def _timestamp(milliseconds: int) -> str:
    return datetime.fromtimestamp(
        milliseconds / 1000,
        tz=UTC,
    ).isoformat().replace("+00:00", "Z")
