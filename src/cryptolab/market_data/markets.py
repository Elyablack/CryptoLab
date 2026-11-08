"""Supported market-data sources and market types."""


DEFAULT_EXCHANGE = "binance"
SPOT_MARKET_TYPE = "spot"
SUPPORTED_EXCHANGES = (DEFAULT_EXCHANGE, "bybit")


def validate_market_identity(exchange: str, market_type: str) -> None:
    """Validate the exchange and market type supported by CryptoLab."""
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError(f"Unsupported exchange: {exchange}")
    if market_type != SPOT_MARKET_TYPE:
        raise ValueError(f"Unsupported market type: {market_type}")
