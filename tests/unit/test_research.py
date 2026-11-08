from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    calculate_benchmarks,
)
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.models import Candle
from cryptolab.research import StrategyResearchRunner
from cryptolab.research.formatter import format_research_report
from cryptolab.research.models import (
    BreakoutDiagnosis,
    StrategyResearchReport,
)
from cryptolab.strategies import Signal, StrategyRunner, TrendFollowingStrategy
from cryptolab.strategies.base import make_decision


HOUR_MILLISECONDS = 3_600_000


def make_candles(count: int) -> list[Candle]:
    candles = []
    for index in range(count):
        open_time = index * HOUR_MILLISECONDS
        candles.append(Candle(
            open_time=open_time,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
            close_time=open_time + HOUR_MILLISECONDS - 1,
            quote_volume=Decimal("1000"),
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ))
    return candles


class ScriptedResearchStrategy:
    name = "trend_following"
    minimum_history = 1

    def __init__(self, signals: Sequence[Signal]) -> None:
        self._signals = tuple(signals)

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: Sequence[FeatureRow],
    ):
        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=history[-1],
            signal=self._signals[len(history) - 1],
            explanation="deterministic exit reason",
            diagnostics={
                "close": Decimal("100"),
                "sma20": Decimal("99"),
                "sma50": Decimal("98"),
                "rsi14": Decimal("55"),
                "rvol20": Decimal("1.2"),
                "volume_confirmed": True,
                "exit_rsi_confirmed": True,
                "exit_policy": "confirmed",
            },
        )


def test_research_runner_preserves_baseline_strategy_behavior() -> None:
    candles = make_candles(72)
    config = BacktestConfig()
    as_of_time = candles[-1].close_time + 1

    research = StrategyResearchRunner(config).run(
        "BTCUSDT",
        "1h",
        candles,
        exchange="bybit",
        as_of_time=as_of_time,
    )
    rows = FeatureBuilder().build(
        candles,
        interval="1h",
        as_of_time=as_of_time,
    )
    direct = BacktestEngine(config).run(
        TrendFollowingStrategy(),
        "BTCUSDT",
        "1h",
        rows,
        exchange="bybit",
        as_of_time=as_of_time,
    )

    assert research.strategy_reports[0] == direct
    assert [report.strategy_name for report in research.strategy_reports] == [
        "trend_following",
        "mean_reversion",
        "breakout",
    ]
    assert [benchmark.name for benchmark in research.benchmarks] == [
        "buy_and_hold",
        "cash_only",
    ]


def test_research_is_deterministic_and_future_period_isolation_is_explicit() -> None:
    candles = make_candles(73)
    runner = StrategyResearchRunner()
    period = candles[:72]
    as_of_time = period[-1].close_time + 1

    first = runner.run(
        "BTCUSDT",
        "1h",
        period,
        as_of_time=as_of_time,
    )
    candles[72] = replace(
        candles[72],
        open=Decimal("1000"),
        high=Decimal("1001"),
        low=Decimal("999"),
        close=Decimal("1000"),
    )
    second = runner.run(
        "BTCUSDT",
        "1h",
        candles[:72],
        as_of_time=as_of_time,
    )

    assert first == second
    assert first.start_time == 0
    assert first.end_time == 72 * HOUR_MILLISECONDS


def test_research_diagnostics_and_formatter_are_reproducible() -> None:
    candles = make_candles(72)
    report = StrategyResearchRunner().run(
        "BTCUSDT",
        "1h",
        candles,
        as_of_time=candles[-1].close_time + 1,
    )

    assert all(
        condition.evaluated == 52
        for condition in report.mean_reversion_conditions
    )
    assert report.breakout_diagnosis.buy_signals == 0
    assert report.breakout_diagnosis.sell_signals == 0

    output = format_research_report(report)
    assert "Baseline performance" in output
    assert "trend_following:" in output
    assert "buy_and_hold:" in output
    assert "cash_only:" in output
    assert "Mean Reversion BUY-condition frequency" in output
    assert "Breakout signal diagnosis" in output
    assert "No completed Trend Following trades." in output


def test_trade_level_research_links_decisions_without_lookahead() -> None:
    candles = make_candles(3)
    rows = FeatureBuilder().build(
        candles,
        interval="1h",
        as_of_time=3 * HOUR_MILLISECONDS,
    )
    strategy = ScriptedResearchStrategy([
        Signal.BUY,
        Signal.SELL,
        Signal.HOLD,
    ])
    config = BacktestConfig(
        fee_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
    )
    backtest = BacktestEngine(config).run(
        strategy,
        "BTCUSDT",
        "1h",
        rows,
        as_of_time=3 * HOUR_MILLISECONDS,
    )
    decisions = StrategyRunner([strategy]).run(
        "BTCUSDT",
        "1h",
        rows,
        as_of_time=3 * HOUR_MILLISECONDS,
    )
    linked = StrategyResearchRunner._link_trend_trades(
        backtest.completed_trades,
        decisions,
    )
    report = StrategyResearchReport(
        exchange="binance",
        market_type="spot",
        symbol="BTCUSDT",
        interval="1h",
        start_time=0,
        end_time=3 * HOUR_MILLISECONDS,
        candle_count=3,
        strategy_reports=(backtest,),
        benchmarks=calculate_benchmarks(
            candles,
            "BTCUSDT",
            "1h",
            config=config,
            as_of_time=3 * HOUR_MILLISECONDS,
        ),
        trend_trades=linked,
        trend_open_position=None,
        mean_reversion_conditions=(),
        breakout_diagnosis=BreakoutDiagnosis(0, 0, 0, 0, 0, 0),
    )

    assert linked[0].entry_decision is decisions[0]
    assert linked[0].exit_decision is decisions[1]
    assert linked[0].holding_milliseconds == HOUR_MILLISECONDS

    output = format_research_report(report)
    assert "signal_candle=1970-01-01T00:00:00Z" in output
    assert "available=1970-01-01T01:00:00Z" in output
    assert "executed=1970-01-01T01:00:00Z" in output
    assert "sma20=99" in output
    assert "holding=1h" in output
    assert "Exit reason: deterministic exit reason" in output

