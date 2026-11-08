"""Simple deterministic benchmarks for strategy research."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from cryptolab.backtesting.models import BacktestConfig
from cryptolab.market_data.markets import (
    DEFAULT_EXCHANGE,
    SPOT_MARKET_TYPE,
    validate_market_identity,
)
from cryptolab.market_data.models import Candle
from cryptolab.market_data.validator import validate_historical_candles


@dataclass(frozen=True)
class BenchmarkReport:
    """Performance of a non-strategy reference portfolio."""

    name: str
    exchange: str
    market_type: str
    symbol: str
    interval: str
    start_time: int
    end_time: int
    config: BacktestConfig
    final_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    maximum_equity_drawdown: Decimal
    fees_paid: Decimal
    exposure_percentage: Decimal
    completed_trade_count: int
    win_rate: Decimal | None
    has_open_position: bool

    @property
    def total_return(self) -> Decimal:
        return self.total_pnl / self.config.initial_cash


def calculate_benchmarks(
    candles: Sequence[Candle],
    symbol: str,
    interval: str,
    *,
    config: BacktestConfig | None = None,
    exchange: str = DEFAULT_EXCHANGE,
    market_type: str = SPOT_MARKET_TYPE,
    as_of_time: int | None = None,
) -> tuple[BenchmarkReport, BenchmarkReport]:
    """Return buy-and-hold and cash-only reports for one evaluation range.

    Buy-and-hold acquires the base asset at the first candle open using the
    same all-in fee and adverse-slippage model as the BacktestEngine. The open
    position is marked at the last close without a hypothetical exit.
    """
    validate_market_identity(exchange, market_type)
    if not symbol.endswith("USDT"):
        raise ValueError("Benchmarks require a USDT-quoted symbol")
    if not candles:
        raise ValueError("At least one candle is required for benchmarks")

    effective_config = config or BacktestConfig()
    evaluation_time = (
        candles[-1].close_time + 1
        if as_of_time is None
        else as_of_time
    )
    validate_historical_candles(
        candles,
        interval,
        as_of_time=evaluation_time,
    )

    buy_and_hold = _calculate_buy_and_hold(
        candles,
        symbol,
        interval,
        effective_config,
        exchange,
        market_type,
    )
    cash_only = BenchmarkReport(
        name="cash_only",
        exchange=exchange,
        market_type=market_type,
        symbol=symbol,
        interval=interval,
        start_time=candles[0].open_time,
        end_time=candles[-1].close_time + 1,
        config=effective_config,
        final_equity=effective_config.initial_cash,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        total_pnl=Decimal("0"),
        maximum_equity_drawdown=Decimal("0"),
        fees_paid=Decimal("0"),
        exposure_percentage=Decimal("0"),
        completed_trade_count=0,
        win_rate=None,
        has_open_position=False,
    )
    return buy_and_hold, cash_only


def _calculate_buy_and_hold(
    candles: Sequence[Candle],
    symbol: str,
    interval: str,
    config: BacktestConfig,
    exchange: str,
    market_type: str,
) -> BenchmarkReport:
    execution_price = candles[0].open * (
        Decimal("1") + config.slippage_rate
    )
    quote_notional = config.initial_cash / (
        Decimal("1") + config.fee_rate
    )
    entry_fee = config.initial_cash - quote_notional
    base_quantity = quote_notional / execution_price
    equities = [base_quantity * candle.close for candle in candles]
    final_equity = equities[-1]
    total_pnl = final_equity - config.initial_cash

    peak = config.initial_cash
    maximum_drawdown = Decimal("0")
    for equity in equities:
        if equity > peak:
            peak = equity
        drawdown = (equity - peak) / peak
        if drawdown < maximum_drawdown:
            maximum_drawdown = drawdown

    return BenchmarkReport(
        name="buy_and_hold",
        exchange=exchange,
        market_type=market_type,
        symbol=symbol,
        interval=interval,
        start_time=candles[0].open_time,
        end_time=candles[-1].close_time + 1,
        config=config,
        final_equity=final_equity,
        realized_pnl=Decimal("0"),
        unrealized_pnl=total_pnl,
        total_pnl=total_pnl,
        maximum_equity_drawdown=maximum_drawdown,
        fees_paid=entry_fee,
        exposure_percentage=Decimal("1"),
        completed_trade_count=0,
        win_rate=None,
        has_open_position=True,
    )
