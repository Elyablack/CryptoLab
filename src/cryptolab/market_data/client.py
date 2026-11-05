"""HTTP client for public Binance Spot market data."""

from typing import Any

import httpx


class BinanceSpotClient:
    """Fetch public market data from Binance Spot."""

    KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
    DEFAULT_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(timeout)
        self._transport = transport

    def get_klines(self, symbol: str, interval: str, limit: int) -> Any:
        """Return the unmodified JSON response for a kline request."""
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }

        with httpx.Client(
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            response = client.get(self.KLINES_URL, params=params)
            response.raise_for_status()
            return response.json()

