import httpx
import pytest

from cryptolab.market_data.bybit_client import BybitSpotClient


def test_get_klines_uses_public_v5_spot_parameters() -> None:
    payload = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"category": "spot", "symbol": "BTCUSDT", "list": []},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.copy_with(query=None) == httpx.URL(
            BybitSpotClient.KLINES_URL
        )
        assert dict(request.url.params) == {
            "category": "spot",
            "symbol": "BTCUSDT",
            "interval": "60",
            "limit": "1000",
            "start": "1704067200000",
            "end": "1704153599999",
        }
        assert "X-BAPI-API-KEY" not in request.headers
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "read": 10.0,
            "write": 10.0,
            "pool": 10.0,
        }
        return httpx.Response(200, json=payload)

    client = BybitSpotClient(transport=httpx.MockTransport(handler))

    result = client.get_klines(
        "BTCUSDT",
        "1h",
        1000,
        start_time=1_704_067_200_000,
        end_time=1_704_153_599_999,
    )

    assert result == payload


def test_get_klines_raises_for_http_error() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, json={"retMsg": "Forbidden"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        BybitSpotClient(transport=transport).get_klines("BTCUSDT", "1h", 1)


def test_get_klines_rejects_unsupported_interval_before_http() -> None:
    with pytest.raises(ValueError, match="Unsupported Bybit interval: 2h"):
        BybitSpotClient().get_klines("BTCUSDT", "2h", 1)
