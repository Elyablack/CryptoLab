from decimal import Decimal

from cryptolab.strategies import Signal, StrategyDecision
from scripts.research_volatility_compression_breakout import (
    MINIMUM_PERIOD_TRADES,
    StudyMetrics,
    calculate_signal_diagnostics,
    evaluate_candidate,
    fixed_config,
)


def make_metrics(**changes) -> StudyMetrics:
    values = {
        "final_equity": Decimal("1100"),
        "total_return": Decimal("0.10"),
        "maximum_drawdown": Decimal("-0.20"),
        "profit_factor": Decimal("1.2"),
        "completed_trades": 40,
        "win_rate": Decimal("0.5"),
        "fees_paid": Decimal("10"),
        "slippage_cost": Decimal("5"),
        "average_trade_pnl": Decimal("2.5"),
        "maximum_win_concentration": Decimal("0.2"),
        "exit_attribution": (("strategy_sell", 40, Decimal("100")),),
    }
    values.update(changes)
    return StudyMetrics(**values)


def make_decision(
    index: int,
    signal: Signal,
    *,
    compression: bool,
    expansion: bool,
    blocker: str,
) -> StrategyDecision:
    return StrategyDecision(
        strategy_name="volatility_compression_breakout_v1",
        symbol="BTCUSDT",
        interval="1h",
        signal=signal,
        candle_open_time=index * 3_600_000,
        signal_available_time=(index + 1) * 3_600_000,
        reference_price=Decimal("100"),
        explanation="test decision",
        diagnostics={
            "compression_confirmed": compression,
            "upper_breakout_confirmed": signal is Signal.BUY,
            "new_upper_breakout": signal is Signal.BUY,
            "bullish_candle_confirmed": True,
            "atr_expansion_confirmed": expansion,
            "volume_confirmed": signal is Signal.BUY,
            "buy_blocker": blocker,
        },
    )


def test_fixed_config_preserves_approved_costs_and_stop() -> None:
    config = fixed_config()

    assert config.initial_cash == Decimal("1000")
    assert config.fee_rate == Decimal("0.001")
    assert config.slippage_rate == Decimal("0.0005")
    assert config.stop_loss_rate == Decimal("0.03")


def test_signal_diagnostics_separate_compression_and_expansion() -> None:
    decisions = (
        make_decision(
            0,
            Signal.HOLD,
            compression=True,
            expansion=False,
            blocker="atr_expansion",
        ),
        make_decision(
            1,
            Signal.HOLD,
            compression=False,
            expansion=True,
            blocker="compression",
        ),
        make_decision(
            2,
            Signal.BUY,
            compression=True,
            expansion=True,
            blocker="none",
        ),
        make_decision(
            3,
            Signal.SELL,
            compression=False,
            expansion=False,
            blocker="compression",
        ),
    )

    result = calculate_signal_diagnostics(decisions)

    assert result.evaluated_rows == 4
    assert result.buy_signals == 1
    assert result.sell_signals == 1
    assert result.compression_and_expansion == 1
    assert result.compression_only == 1
    assert result.expansion_only == 1
    assert result.neither_compression_nor_expansion == 1
    assert dict(result.sequential_blockers) == {
        "atr_expansion": 1,
        "compression": 2,
    }


def test_candidate_requires_both_periods_to_pass_every_fixed_gate() -> None:
    assert evaluate_candidate(make_metrics(), make_metrics()) == ()

    failures = evaluate_candidate(
        make_metrics(maximum_drawdown=Decimal("-0.26")),
        make_metrics(
            total_return=Decimal("-0.01"),
            profit_factor=Decimal("0.9"),
            completed_trades=MINIMUM_PERIOD_TRADES - 1,
            maximum_win_concentration=Decimal("0.26"),
        ),
    )

    assert "development: drawdown is worse than -25%" in failures
    assert "validation: net return is not positive" in failures
    assert "validation: profit factor is not above 1" in failures
    assert "validation: fewer than 30 completed trades" in failures
    assert "validation: largest win exceeds 25% of gross profit" in failures
