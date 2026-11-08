"""HTTP client for public Binance Spot market data."""

from collections.abc import Callable
from typing import Any

import httpx

from cryptolab.market_data.http import RetryPolicy, get_public_json


class BinanceSpotClient:
    """Fetch public market data from Binance Spot."""

    KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
    DEFAULT_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(timeout)
        self._transport = transport
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._jitter = jitter

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> Any:
        """Return the unmodified JSON response for a kline request."""
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time

        kwargs: dict[str, object] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        if self._jitter is not None:
            kwargs["jitter"] = self._jitter
        return get_public_json(
            exchange="binance",
            url=self.KLINES_URL,
            params=params,
            timeout=self._timeout,
            transport=self._transport,
            retry_policy=self._retry_policy,
            **kwargs,
        )

