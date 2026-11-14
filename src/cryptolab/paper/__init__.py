"""Continuous public-data paper trading; never real order execution."""

from cryptolab.paper.execution import (
    PaperExecutionResult,
    PaperExecutionSimulator,
    PaperPositionSizer,
)
from cryptolab.paper.market import (
    BybitPaperMarketSource,
    PaperMarketSource,
    parse_bybit_last_price,
)
from cryptolab.paper.models import (
    MultiSymbolPaperBotConfig,
    MultiSymbolPaperCycleResult,
    MultiSymbolPaperSessionStatus,
    PaperBotConfig,
    PaperCycleResult,
    PaperExecutionConfig,
    PaperRiskConfig,
    PaperSessionStatus,
    PendingRotation,
    PortfolioReconciliationReport,
    RotationPhase,
)
from cryptolab.paper.portfolio_service import (
    MultiSymbolPaperTrader,
    set_portfolio_paper_trading_pause,
)
from cryptolab.paper.risk import PaperRiskManager
from cryptolab.paper.service import (
    ContinuousPaperTrader,
    set_paper_trading_pause,
)

__all__ = [
    "BybitPaperMarketSource",
    "ContinuousPaperTrader",
    "MultiSymbolPaperBotConfig",
    "MultiSymbolPaperCycleResult",
    "MultiSymbolPaperSessionStatus",
    "MultiSymbolPaperTrader",
    "PaperBotConfig",
    "PaperCycleResult",
    "PaperExecutionConfig",
    "PaperExecutionResult",
    "PaperExecutionSimulator",
    "PaperMarketSource",
    "PaperPositionSizer",
    "PaperRiskConfig",
    "PaperRiskManager",
    "PaperSessionStatus",
    "PendingRotation",
    "PortfolioReconciliationReport",
    "RotationPhase",
    "parse_bybit_last_price",
    "set_paper_trading_pause",
    "set_portfolio_paper_trading_pause",
]
