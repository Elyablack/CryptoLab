"""HTTP client for public Bybit V5 Spot market data."""

from typing import Any

import httpx


class BybitSpotClient:
    """Fetch public Bybit V5 Spot klines without authentication."""

    KLINES_URL = "https://api.bybit.com/v5/market/kline"
    DEFAULT_TIMEOUT_SECONDS = 10.0
    INTERVALS = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "1h": "60",
        "4h": "240",
        "1d": "D",
    }

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(timeout)
        self._transport = transport

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> Any:
        """Return the unmodified public V5 kline JSON response."""
        try:
            bybit_interval = self.INTERVALS[interval]
        except KeyError as error:
            raise ValueError(f"Unsupported Bybit interval: {interval}") from error

        params: dict[str, str | int] = {
            "category": "spot",
            "symbol": symbol,
            "interval": bybit_interval,
            "limit": limit,
        }
        if start_time is not None:
            params["start"] = start_time
        if end_time is not None:
            params["end"] = end_time

        with httpx.Client(
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            response = client.get(self.KLINES_URL, params=params)
            response.raise_for_status()
            return response.json()
