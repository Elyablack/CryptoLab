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
from cryptolab.paper.notifications import (
    NotificationOutbox,
    TelegramNotifier,
)
from cryptolab.paper.observability import (
    JsonLogFormatter,
    MetricsServer,
    PaperMetrics,
    PaperTelemetry,
    configure_json_logging,
)
from cryptolab.paper.operations import (
    DatabaseVerification,
    PaperHealthReport,
    backup_sqlite,
    paper_health,
    restore_sqlite,
    verify_sqlite_database,
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
    "NotificationOutbox",
    "PaperBotConfig",
    "PaperCycleResult",
    "PaperExecutionConfig",
    "PaperExecutionResult",
    "PaperExecutionSimulator",
    "PaperMarketSource",
    "PaperPositionSizer",
    "PaperMetrics",
    "PaperTelemetry",
    "PaperRiskConfig",
    "PaperRiskManager",
    "PaperSessionStatus",
    "PendingRotation",
    "PortfolioReconciliationReport",
    "RotationPhase",
    "TelegramNotifier",
    "JsonLogFormatter",
    "MetricsServer",
    "DatabaseVerification",
    "PaperHealthReport",
    "backup_sqlite",
    "configure_json_logging",
    "paper_health",
    "parse_bybit_last_price",
    "set_paper_trading_pause",
    "set_portfolio_paper_trading_pause",
    "restore_sqlite",
    "verify_sqlite_database",
]
