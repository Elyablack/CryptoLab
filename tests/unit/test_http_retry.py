import httpx
import pytest

from cryptolab.market_data.bybit_client import BybitSpotClient
from cryptolab.market_data.client import BinanceSpotClient
from cryptolab.market_data.http import (
    ExchangeAccessBlockedError,
    RetryExhaustedError,
    RetryPolicy,
)


def _policy(attempts: int = 3) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=attempts,
        base_delay_seconds=0.25,
        maximum_delay_seconds=2,
        jitter_fraction=0,
    )


def test_retries_transient_server_error_then_succeeds() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=[])

    client = BinanceSpotClient(
        transport=httpx.MockTransport(handler),
        retry_policy=_policy(),
        sleep=delays.append,
        jitter=lambda: 0,
    )

    assert client.get_klines("BTCUSDT", "1h", 1) == []
    assert attempts == 2
    assert delays == [0.25]


def test_retry_after_is_respected_for_http_429() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                request=request,
            )
        return httpx.Response(200, json=[])

    client = BinanceSpotClient(
        transport=httpx.MockTransport(handler),
        retry_policy=_policy(),
        sleep=delays.append,
        jitter=lambda: 0,
    )

    assert client.get_klines("BTCUSDT", "1h", 1) == []
    assert delays == [2.0]


def test_timeout_retry_exhaustion_is_clear_and_bounded() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = BinanceSpotClient(
        transport=httpx.MockTransport(handler),
        retry_policy=_policy(),
        sleep=delays.append,
        jitter=lambda: 0,
    )

    with pytest.raises(RetryExhaustedError, match="after 3 attempts"):
        client.get_klines("BTCUSDT", "1h", 1)

    assert attempts == 3
    assert delays == [0.25, 0.5]


def test_non_retryable_client_error_fails_immediately() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, request=request)

    client = BinanceSpotClient(
        transport=httpx.MockTransport(handler),
        retry_policy=_policy(),
        sleep=lambda delay: pytest.fail("must not retry HTTP 400"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.get_klines("BTCUSDT", "1h", 1)
    assert attempts == 1


def test_binance_http_418_stops_without_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(418, request=request)

    client = BinanceSpotClient(
        transport=httpx.MockTransport(handler),
        retry_policy=_policy(),
        sleep=lambda delay: pytest.fail("must not retry HTTP 418"),
    )

    with pytest.raises(ExchangeAccessBlockedError, match="HTTP 418"):
        client.get_klines("BTCUSDT", "1h", 1)
    assert attempts == 1


def test_bybit_api_rate_limit_code_is_retried() -> None:
    attempts = 0
    delays: list[float] = []
    success = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"category": "spot", "symbol": "BTCUSDT", "list": []},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json={"retCode": 10006})
        return httpx.Response(200, json=success)

    client = BybitSpotClient(
        transport=httpx.MockTransport(handler),
        retry_policy=_policy(),
        sleep=delays.append,
        jitter=lambda: 0,
    )

    assert client.get_klines("BTCUSDT", "1h", 1) == success
    assert attempts == 2
    assert delays == [0.25]


def test_server_error_retry_exhaustion_reports_status() -> None:
    client = BybitSpotClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        ),
        retry_policy=_policy(2),
        sleep=lambda delay: None,
        jitter=lambda: 0,
    )

    with pytest.raises(RetryExhaustedError, match="HTTP 500 after 2 attempts"):
        client.get_klines("BTCUSDT", "1h", 1)
