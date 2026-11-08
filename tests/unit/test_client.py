import httpx
import pytest

from cryptolab.market_data.client import BinanceSpotClient
from cryptolab.market_data.http import RetryExhaustedError, RetryPolicy


def test_get_klines_returns_json_and_sends_expected_request() -> None:
    payload = [
        [
            1_700_000_000_000,
            "35000.00",
            "35500.00",
            "34900.00",
            "35200.00",
            "123.45",
        ]
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.copy_with(query=None) == httpx.URL(
            BinanceSpotClient.KLINES_URL
        )
        assert dict(request.url.params) == {
            "symbol": "BTCUSDT",
            "interval": "1h",
            "limit": "100",
        }
        assert "X-MBX-APIKEY" not in request.headers
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "read": 10.0,
            "write": 10.0,
            "pool": 10.0,
        }
        return httpx.Response(200, json=payload)

    client = BinanceSpotClient(transport=httpx.MockTransport(handler))

    result = client.get_klines(symbol="BTCUSDT", interval="1h", limit=100)

    assert result == payload


def test_get_klines_raises_for_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"code": -1003, "msg": "Too many requests"})

    client = BinanceSpotClient(
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(max_attempts=2),
        sleep=lambda delay: None,
        jitter=lambda: 0,
    )

    with pytest.raises(RetryExhaustedError, match="HTTP 429 after 2 attempts"):
        client.get_klines(symbol="ETHUSDT", interval="1h", limit=50)


def test_get_klines_sends_optional_time_range() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "symbol": "BTCUSDT",
            "interval": "1h",
            "limit": "1000",
            "startTime": "1700000000000",
            "endTime": "1700003599999",
        }
        return httpx.Response(200, json=[])

    client = BinanceSpotClient(transport=httpx.MockTransport(handler))

    result = client.get_klines(
        symbol="BTCUSDT",
        interval="1h",
        limit=1000,
        start_time=1_700_000_000_000,
        end_time=1_700_003_599_999,
    )

    assert result == []
