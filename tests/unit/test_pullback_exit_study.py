from decimal import Decimal

from scripts.research_pullback_exits import (
    MAXIMUM_DRAWDOWN,
    MINIMUM_PERIOD_TRADES,
    StudyMetrics,
    candidate_failures,
    predefined_variants,
)
from cryptolab.strategies import TrendPullbackExitPolicy


def make_metrics(**changes) -> StudyMetrics:
    values = {
        "total_return": Decimal("0.10"),
        "maximum_drawdown": Decimal("-0.20"),
        "profit_factor": Decimal("1.2"),
        "completed_trades": 40,
        "win_rate": Decimal("0.5"),
        "fees_paid": Decimal("10"),
        "slippage_cost": Decimal("5"),
        "average_trade_pnl": Decimal("2.5"),
        "maximum_win_concentration": Decimal("0.2"),
        "average_holding_hours": Decimal("24"),
        "exit_attribution": (("strategy_sell", 40, Decimal("100")),),
    }
    values.update(changes)
    return StudyMetrics(**values)


def test_predefined_study_has_exactly_three_fixed_variants() -> None:
    variants = predefined_variants()

    assert [item.name for item in variants] == [
        "baseline",
        "time_exit_72h",
        "long_term_trend_exit",
    ]
    assert variants[0].strategy.exit_policy is TrendPullbackExitPolicy.BASELINE
    assert (
        variants[1].strategy.exit_policy
        is TrendPullbackExitPolicy.ENTRY_ONLY
    )
    assert variants[1].config.max_holding_candles == 72
    assert (
        variants[2].strategy.exit_policy
        is TrendPullbackExitPolicy.LONG_TERM_TREND
    )
    assert all(item.config.stop_loss_rate == Decimal("0.03") for item in variants)


def test_candidate_passes_only_when_both_periods_meet_every_gate() -> None:
    assert candidate_failures(make_metrics(), make_metrics()) == ()


def test_candidate_failures_are_explicit_and_do_not_use_studied_test() -> None:
    development = make_metrics(
        maximum_drawdown=MAXIMUM_DRAWDOWN - Decimal("0.01"),
    )
    validation = make_metrics(
        total_return=Decimal("-0.01"),
        profit_factor=Decimal("0.9"),
        completed_trades=MINIMUM_PERIOD_TRADES - 1,
        maximum_win_concentration=Decimal("0.3"),
    )

    failures = candidate_failures(development, validation)

    assert "development: drawdown is worse than -25%" in failures
    assert "validation: net return is not positive" in failures
    assert "validation: profit factor is not above 1" in failures
    assert "validation: fewer than 30 completed trades" in failures
    assert "validation: largest win exceeds 25% of gross profit" in failures
