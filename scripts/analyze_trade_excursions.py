
"""Analyze MAE, MFE, and exit timing for Trend Following trades."""

import re
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean

REPORT = Path("reports/baseline-18m.txt")
DATABASE = Path("data/market_data.db")

EXCHANGE = "bybit"
MARKET_TYPE = "spot"
SYMBOL = "BTCUSDT"
INTERVAL = "1h"

HOUR_MS = 3_600_000


def to_ms(timestamp: str) -> int:
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def parse_trades(report: str):
    section = report.split(
        "Trend Following executed trades", 1
    )[1].split(
        "Trend Following remaining open position:", 1
    )[0]

    pattern = re.compile(
        r"Trade (\d+):.*?"
        r"Entry times:.*?executed=([^\s]+).*?"
        r"Entry execution: price=([\d.]+).*?"
        r"Exit times:.*?executed=([^\s]+).*?"
        r"Exit execution: price=([\d.]+).*?"
        r"Result: pnl=([-\d.]+) USDT, "
        r"return=([-\d.]+)%, holding=(\d+)h",
        re.DOTALL,
    )

    for match in pattern.finditer(section):
        (
            number,
            entry_time,
            entry_price,
            exit_time,
            exit_price,
            pnl,
            trade_return,
            holding,
        ) = match.groups()

        yield {
            "trade": int(number),
            "entry_ms": to_ms(entry_time),
            "exit_ms": to_ms(exit_time),
            "entry_price": Decimal(entry_price),
            "exit_price": Decimal(exit_price),
            "pnl": float(pnl),
            "return": float(trade_return),
            "holding": int(holding),
        }


def analyze_trade(connection, trade):
    rows = connection.execute(
        """
        SELECT open_time, high, low
        FROM candles
        WHERE exchange = ?
          AND market_type = ?
          AND symbol = ?
          AND interval = ?
          AND open_time >= ?
          AND open_time < ?
        ORDER BY open_time
        """,
        (
            EXCHANGE,
            MARKET_TYPE,
            SYMBOL,
            INTERVAL,
            trade["entry_ms"],
            trade["exit_ms"],
        ),
    ).fetchall()

    if not rows:
        raise ValueError(
            f"No candles found for trade {trade['trade']}"
        )

    expected = (
        trade["exit_ms"] - trade["entry_ms"]
    ) // HOUR_MS

    if len(rows) != expected:
        raise ValueError(
            f"Trade {trade['trade']}: "
            f"expected {expected} candles, found {len(rows)}"
        )

    # Check continuity and OHLC integrity.
    for index, (open_time, high, low) in enumerate(rows):
        expected_time = trade["entry_ms"] + index * HOUR_MS

        if open_time != expected_time:
            raise ValueError(
                f"Trade {trade['trade']}: candle gap at {index}"
            )

        if Decimal(low) > Decimal(high):
            raise ValueError(
                f"Trade {trade['trade']}: invalid high/low"
            )

    highest_time, highest_value, _ = max(
        rows,
        key=lambda row: Decimal(row[1]),
    )

    lowest_time, _, lowest_value = min(
        rows,
        key=lambda row: Decimal(row[2]),
    )

    highest = Decimal(highest_value)
    lowest = Decimal(lowest_value)

    entry = trade["entry_price"]
    exit_price = trade["exit_price"]

    mfe = (highest - entry) / entry * 100
    mae = (lowest - entry) / entry * 100

    giveback = (highest - exit_price) / entry * 100

    hours_to_mfe = (
        highest_time - trade["entry_ms"]
    ) / HOUR_MS

    hours_to_mae = (
        lowest_time - trade["entry_ms"]
    ) / HOUR_MS

    # Measured from the start of the MFE candle.
    hours_after_mfe = (
        trade["exit_ms"] - highest_time
    ) / HOUR_MS

    if highest_time < lowest_time:
        extreme_order = "MFE first"
    elif lowest_time < highest_time:
        extreme_order = "MAE first"
    else:
        extreme_order = "Same candle"

    return {
        **trade,
        "mae": float(mae),
        "mfe": float(mfe),
        "giveback": float(giveback),
        "hours_to_mfe": hours_to_mfe,
        "hours_to_mae": hours_to_mae,
        "hours_after_mfe": hours_after_mfe,
        "extreme_order": extreme_order,
    }


def print_results(results):
    print(f"Analyzed trades: {len(results)}")

    print("\nTrade excursions:")
    print(
        f"{'Trade':>5} {'Hours':>7} "
        f"{'MAE %':>9} {'MFE %':>9} "
        f"{'Return %':>10} {'PnL':>10}"
    )

    for t in results:
        print(
            f"{t['trade']:>5} "
            f"{t['holding']:>7} "
            f"{t['mae']:>9.2f} "
            f"{t['mfe']:>9.2f} "
            f"{t['return']:>10.2f} "
            f"{t['pnl']:>10.2f}"
        )

    print("\nExit timing analysis:")
    print(
        f"{'Trade':>5} {'MFE hour':>9} "
        f"{'MAE hour':>9} {'After MFE':>10} "
        f"{'Giveback %':>11} {'Return %':>10} "
        f"{'Order':>12}"
    )

    for t in results:
        print(
            f"{t['trade']:>5} "
            f"{t['hours_to_mfe']:>9.0f} "
            f"{t['hours_to_mae']:>9.0f} "
            f"{t['hours_after_mfe']:>10.0f} "
            f"{t['giveback']:>11.2f} "
            f"{t['return']:>10.2f} "
            f"{t['extreme_order']:>12}"
        )

    winners = [t for t in results if t["return"] > 0]
    losers = [t for t in results if t["return"] < 0]

    print("\nSummary:")
    print(
        f"Average MAE: "
        f"{mean(t['mae'] for t in results):.2f}%"
    )
    print(
        f"Average MFE: "
        f"{mean(t['mfe'] for t in results):.2f}%"
    )
    print(f"Winning trades: {len(winners)}")
    print(f"Losing trades: {len(losers)}")

    if losers:
        print(
            f"Average MAE (losers): "
            f"{mean(t['mae'] for t in losers):.2f}%"
        )
        print(
            f"Average MFE (losers): "
            f"{mean(t['mfe'] for t in losers):.2f}%"
        )

    if winners:
        print(
            f"Average MFE (winners): "
            f"{mean(t['mfe'] for t in winners):.2f}%"
        )

    print(
        f"Average giveback: "
        f"{mean(t['giveback'] for t in results):.2f}%"
    )

    for order in ("MFE first", "MAE first", "Same candle"):
        count = sum(
            t["extreme_order"] == order
            for t in results
        )
        print(f"{order}: {count}")

    print(
        "Average hours after MFE: "
        f"{mean(t['hours_after_mfe'] for t in results):.1f}"
    )


def main():
    report = REPORT.read_text(encoding="utf-8")
    trades = list(parse_trades(report))

    if len(trades) != 29:
        raise ValueError(
            f"Expected 29 closed trades, found {len(trades)}"
        )

    # Open SQLite explicitly in read-only mode.
    database_uri = DATABASE.resolve().as_uri() + "?mode=ro"

    with sqlite3.connect(database_uri, uri=True) as connection:
        results = [
            analyze_trade(connection, trade)
            for trade in trades
        ]

    print_results(results)


if __name__ == "__main__":
    main()
