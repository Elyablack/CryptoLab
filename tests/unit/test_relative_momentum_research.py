from decimal import Decimal
from types import SimpleNamespace

from cryptolab.backtesting import BacktestConfig
from cryptolab.market_data.models import Candle
from cryptolab.research.validation import ResearchPeriod
from scripts.research_relative_momentum import (
    NamedMetrics,
    PerformanceMetrics,
    equal_weight_benchmark,
    evaluate_candidate,
)


HOUR = 3_600_000


def candle(index: int, open_price: str, close_price: str) -> Candle:
    open_value = Decimal(open_price)
    close_value = Decimal(close_price)
    return Candle(
        open_time=index * HOUR,
        open=open_value,
        high=max(open_value, close_value) + Decimal("1"),
        low=min(open_value, close_value) - Decimal("1"),
        close=close_value,
        volume=Decimal("10"),
        close_time=(index + 1) * HOUR - 1,
        quote_volume=Decimal("1000"),
        number_of_trades=None,
        taker_buy_base_volume=None,
        taker_buy_quote_volume=None,
    )


def period(name: str = "validation") -> ResearchPeriod:
    return ResearchPeriod(
        name=name,
        start_index=0,
        end_index=2,
        start_time=0,
        end_time=2 * HOUR,
    )


def metrics(
    *,
    total_return: str = "0.1",
    drawdown: str = "-0.1",
    reviews: int = 40,
    changes: int = 6,
    concentration: str = "0.2",
) -> PerformanceMetrics:
    return PerformanceMetrics(
        final_equity=Decimal("1000") * (Decimal("1") + Decimal(total_return)),
        total_return=Decimal(total_return),
        cagr=Decimal(total_return),
        maximum_drawdown=Decimal(drawdown),
        profit_factor=Decimal("1.5"),
        completed_trades=6,
        win_rate=Decimal("0.5"),
        fees_paid=Decimal("2"),
        slippage_cost=Decimal("1"),
        time_exposure=Decimal("0.5"),
        capital_exposure=Decimal("0.5"),
        weekly_reviews=reviews,
        target_changes=changes,
        asset_rotations=2,
        largest_win_concentration=Decimal(concentration),
        gross_profit=Decimal("100"),
        gross_loss=Decimal("50"),
    )


def period_study(
    primary: PerformanceMetrics,
    equal_weight: PerformanceMetrics,
):
    return SimpleNamespace(
        primary=SimpleNamespace(metrics=primary),
        half_exposure=SimpleNamespace(metrics=metrics(total_return="9")),
        benchmarks=(NamedMetrics("equal_weight_btc_eth", equal_weight),),
    )


def test_equal_weight_benchmark_uses_two_independent_half_allocations() -> None:
    btc = (candle(0, "100", "100"), candle(1, "100", "110"))
    eth = (candle(0, "50", "50"), candle(1, "50", "45"))
    config = BacktestConfig(
        fee_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
    )

    result = equal_weight_benchmark(btc, eth, period(), config)

    assert result.final_equity == Decimal("1000")
    assert result.total_return == 0
    assert result.fees_paid == 0
    assert result.slippage_cost == 0
    assert result.maximum_drawdown == 0
    assert result.time_exposure == 1


def test_equal_weight_benchmark_charges_entry_fee_and_slippage() -> None:
    btc = (candle(0, "100", "100"), candle(1, "100", "100"))
    eth = (candle(0, "50", "50"), candle(1, "50", "50"))
    config = BacktestConfig(
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.0005"),
    )

    result = equal_weight_benchmark(btc, eth, period(), config)

    assert result.final_equity < Decimal("1000")
    assert result.fees_paid > 0
    assert result.slippage_cost > 0


def test_candidate_gates_use_primary_development_and_validation_only() -> None:
    development = period_study(metrics(reviews=30), metrics(total_return="0.05"))
    validation = period_study(
        metrics(reviews=10),
        metrics(total_return="0.05", drawdown="-0.15"),
    )

    checks = evaluate_candidate(development, validation)

    assert all(check.passed for check in checks)


def test_candidate_gates_reject_validation_loss_risk_and_concentration() -> None:
    development = period_study(metrics(reviews=30), metrics())
    validation = period_study(
        metrics(
            total_return="-0.1",
            drawdown="-0.3",
            reviews=10,
            concentration="0.4",
        ),
        metrics(total_return="0.05", drawdown="-0.2"),
    )

    checks = evaluate_candidate(development, validation)
    failed = {check.name for check in checks if not check.passed}

    assert "Validation: positive net return" in failed
    assert "Validation: max drawdown >= -25%" in failed
    assert "Validation: largest win <= 25% gross profit" in failed
    assert (
        "Validation: not dominated by equal-weight return and drawdown"
        in failed
    )
