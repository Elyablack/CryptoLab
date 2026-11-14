# Trend Pullback Strategy v1

## Status

**Rejected as implemented.** This is exploratory evidence, not an independent
holdout result and not a statement that all pullback strategies are invalid.
The original three CryptoLab strategies and their parameters are unchanged.

## Predeclared hypothesis and rules

The candidate tests whether a confirmed short pullback in an established
BTCUSDT 1h uptrend can produce useful long-only Spot entries.

An uptrend requires all of the following on the signal candle:

- SMA20 is above SMA50;
- close is above SMA50;
- SMA50 is above its value six candles earlier.

A qualifying pullback must occur in one of the three candles strictly before
the signal candle:

- low touches or crosses EMA20;
- close remains above SMA50;
- RSI14 is between 40 and 55 inclusive;
- the same pullback has not already produced a recovery event.

Recovery confirmation requires:

- close above the immediately preceding high and above EMA20;
- a positive simple return;
- RSI14 between 50 and 65 inclusive;
- RVOL20 at least 0.8;
- ATR14 divided by close no greater than 2.5%.

BUY is a quantity-free strategy decision available after that candle closes.
The legacy all-in Spot portfolio executes it at the next candle open with the
configured fee and adverse slippage. The engine rejects repeated BUY while a
position is already open.

A normal strategy SELL is emitted for a bearish SMA20/SMA50 crossover, or when
price crosses below SMA50 while SMA20 is falling. SELL means exit long Spot
exposure, never open a short.

The experiment uses a fixed protective stop 3% below the actual entry
execution price. A pending strategy SELL executes first at the candle open.
Otherwise, a gap through the stop exits at the open; an intrabar low crossing
exits at the stop price. Existing adverse slippage and fees apply to both. The
stop is active on the entry candle and never uses a future candle.

## Dataset and execution assumptions

- Source: Bybit Spot BTCUSDT 1h.
- Range: `[2023-11-01T00:00:00Z, 2026-11-01T00:00:00Z)`.
- Candles: 26,304 expected and present; no missing, unexpected, duplicate, or
  invalid rows according to `cryptolab quality`.
- Initial cash: 1,000 USDT.
- Fee: 0.1% per fill.
- Adverse slippage: 0.05% per fill.
- No forced final exit; open exposure would be marked at the final close.

## Exploratory result

| Metric | Trend Pullback v1 | Buy and Hold | Cash |
|---|---:|---:|---:|
| Final equity | 652.0454 USDT | 2,234.7330 USDT | 1,000 USDT |
| Net return | -34.7955% | +123.4733% | 0% |
| Maximum close-equity drawdown | -45.4891% | -53.7338% | 0% |
| Completed trades | 222 | 0 (position remains open) | 0 |
| Win rate | 23.42% | N/A | N/A |
| Profit factor | 0.7670 | N/A | N/A |
| Average winning trade | +22.0283 USDT | N/A | N/A |
| Average losing trade | -8.7849 USDT | N/A | N/A |
| Fees | 366.5147 USDT | entry fee only | 0 USDT |
| Protective exits | 6 | N/A | N/A |

The candidate traded frequently enough that fees were economically material,
but the gross losing-trade balance also exceeded the winning-trade balance:
removing fees from interpretation would not turn a profit factor below one
into evidence of an effective rule.

### Chronological 60/20/20 diagnostics

The final segment is called `studied_test`: it was already used by prior
CryptoLab research and is not a new independent holdout.

| Period | UTC range | Trades | Return | Max drawdown | Profit factor | Buy and Hold |
|---|---|---:|---:|---:|---:|---:|
| Development | 2023-11-01 to 2025-08-19 14:00 | 139 | -17.6651% | -31.1576% | 0.8286 | +232.0544% |
| Validation | 2025-08-19 14:00 to 2026-03-26 18:00 | 34 | -5.4089% | -11.5716% | 0.7233 | -40.6378% |
| Studied test | 2026-03-26 18:00 to 2026-11-01 | 49 | -16.2772% | -24.6843% | 0.5803 | +13.0328% |

Fresh portfolios start at each boundary, while all earlier feature rows remain
available only for indicator warm-up. Positions are not force-closed at a
boundary.

### One-year stability diagnostics

| UTC range | Trades | Return | Max drawdown | Profit factor | Buy and Hold |
|---|---:|---:|---:|---:|---:|
| 2023-11-01 to 2024-11-01 | 77 | -13.8023% | -31.1576% | 0.7343 | +102.6485% |
| 2024-11-01 to 2025-11-01 | 71 | -1.3588% | -26.2870% | 0.9780 | +55.6632% |
| 2025-11-01 to 2026-11-01 | 74 | -23.3126% | -31.4502% | 0.5500 | -29.3692% |

All three yearly diagnostics are negative. The result therefore does not
support advancing this exact rule set toward paper or live execution.

## Reproduction

```bash
cryptolab quality \
  --exchange bybit --symbol BTCUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 \
  --database data/market_data.db

cryptolab backtest \
  --exchange bybit --symbol BTCUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 \
  --database data/market_data.db \
  --strategy trend-pullback --stop-loss-rate 0.03 --show-trades
```

The CLI full-range backtest is reproducible. The period tables use the existing
`create_chronological_split` and `PeriodRestrictedStrategy` research utilities;
they are diagnostics, not parameter selection.

## Limitations

- This is one asset, one interval, and one already explored historical range.
- The fixed 3% stop was a predeclared modeling choice, not an optimized value.
- Hourly OHLC does not expose the path within a candle. An intrabar stop fill at
  the stop price plus fixed slippage is an assumption, not an order-book fill.
- Lot size, tick size, minimum notional, latency, and partial exchange fills are
  not included in this candle backtest.
- Buy and Hold accepts substantially greater drawdown and continuous exposure,
  so its return is a reference rather than a risk-matched strategy.
- No independent future holdout exists. A different hypothesis would require a
  new predeclared candidate and new data, not threshold tuning on this result.
