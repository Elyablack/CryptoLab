"""Supported market-data intervals."""


INTERVAL_MILLISECONDS: dict[str, int] = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


def get_interval_milliseconds(interval: str) -> int:
    """Return the duration of a supported interval."""
    try:
        return INTERVAL_MILLISECONDS[interval]
    except KeyError as error:
        raise ValueError(f"Unsupported interval: {interval}") from error

