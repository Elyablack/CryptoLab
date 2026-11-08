"""Reliable HTTP execution for public exchange market-data requests."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


LOGGER = logging.getLogger(__name__)


class MarketDataRequestError(RuntimeError):
    """Base error for a classified public market-data request failure."""


class RetryExhaustedError(MarketDataRequestError):
    """Raised after all attempts for a transient failure are exhausted."""


class ExchangeAccessBlockedError(MarketDataRequestError):
    """Raised when an exchange explicitly blocks further requests."""


class InvalidMarketDataResponseError(MarketDataRequestError):
    """Raised when an HTTP response cannot be decoded as JSON."""


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential-backoff policy.

    ``max_attempts`` includes the initial request. Jitter is a fraction of the
    computed delay and is added, never subtracted, so a server-provided
    ``Retry-After`` value is always respected as a minimum.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    maximum_delay_seconds: float = 5.0
    jitter_fraction: float = 0.10

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts <= 0:
            raise ValueError("max_attempts must be a positive integer")
        for name, value in (
            ("base_delay_seconds", self.base_delay_seconds),
            ("maximum_delay_seconds", self.maximum_delay_seconds),
            ("jitter_fraction", self.jitter_fraction),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.maximum_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                "maximum_delay_seconds must be at least base_delay_seconds"
            )


def get_public_json(
    *,
    exchange: str,
    url: str,
    params: Mapping[str, str | int],
    timeout: httpx.Timeout,
    transport: httpx.BaseTransport | None,
    retry_policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    retry_json_codes: frozenset[int] = frozenset(),
) -> Any:
    """GET and decode JSON with bounded exchange-aware retries."""
    with httpx.Client(timeout=timeout, transport=transport) as client:
        for attempt in range(1, retry_policy.max_attempts + 1):
            try:
                response = client.get(url, params=params)
            except httpx.TransportError as error:
                if attempt == retry_policy.max_attempts:
                    raise RetryExhaustedError(
                        f"{exchange} request failed after {attempt} attempts: "
                        f"{type(error).__name__}"
                    ) from error
                _wait_before_retry(
                    exchange=exchange,
                    attempt=attempt,
                    reason=type(error).__name__,
                    policy=retry_policy,
                    sleep=sleep,
                    jitter=jitter,
                )
                continue

            status = response.status_code
            if exchange == "binance" and status == 418:
                raise ExchangeAccessBlockedError(
                    "Binance returned HTTP 418; collection stopped to avoid "
                    "extending the IP restriction"
                )

            if status == 429 or 500 <= status < 600:
                if attempt == retry_policy.max_attempts:
                    raise RetryExhaustedError(
                        f"{exchange} HTTP {status} after {attempt} attempts"
                    )
                _wait_before_retry(
                    exchange=exchange,
                    attempt=attempt,
                    reason=f"HTTP {status}",
                    policy=retry_policy,
                    sleep=sleep,
                    jitter=jitter,
                    minimum_delay=_retry_after_seconds(response, exchange),
                )
                continue

            # All other 4xx responses are request errors and must fail now.
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as error:
                raise InvalidMarketDataResponseError(
                    f"{exchange} returned invalid JSON"
                ) from error

            response_code = _integer_response_code(payload)
            if response_code in retry_json_codes:
                if attempt == retry_policy.max_attempts:
                    raise RetryExhaustedError(
                        f"{exchange} API code {response_code} after "
                        f"{attempt} attempts"
                    )
                _wait_before_retry(
                    exchange=exchange,
                    attempt=attempt,
                    reason=f"API code {response_code}",
                    policy=retry_policy,
                    sleep=sleep,
                    jitter=jitter,
                    minimum_delay=_retry_after_seconds(response, exchange),
                )
                continue

            return payload

    raise RuntimeError("Unreachable retry loop state")


def _wait_before_retry(
    *,
    exchange: str,
    attempt: int,
    reason: str,
    policy: RetryPolicy,
    sleep: Callable[[float], None],
    jitter: Callable[[], float],
    minimum_delay: float | None = None,
) -> None:
    exponential_delay = min(
        policy.maximum_delay_seconds,
        policy.base_delay_seconds * (2 ** (attempt - 1)),
    )
    delay = max(exponential_delay, minimum_delay or 0.0)
    random_value = jitter()
    if not 0 <= random_value <= 1:
        raise ValueError("jitter callback must return a value between 0 and 1")
    delay += delay * policy.jitter_fraction * random_value
    LOGGER.warning(
        "%s public API retry after %s (attempt %d/%d, delay %.3fs)",
        exchange,
        reason,
        attempt,
        policy.max_attempts,
        delay,
    )
    sleep(delay)


def _retry_after_seconds(
    response: httpx.Response,
    exchange: str,
) -> float | None:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(
                    0.0,
                    (retry_at - datetime.now(UTC)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                LOGGER.warning(
                    "%s returned an invalid Retry-After header", exchange
                )

    if exchange == "bybit":
        reset_milliseconds = response.headers.get(
            "X-Bapi-Limit-Reset-Timestamp"
        )
        if reset_milliseconds:
            try:
                reset = int(reset_milliseconds) / 1000
                return max(0.0, reset - time.time())
            except ValueError:
                LOGGER.warning(
                    "Bybit returned an invalid rate-limit reset timestamp"
                )
    return None


def _integer_response_code(payload: object) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("retCode")
    return value if type(value) is int else None
