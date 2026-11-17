# Volatility Compression Breakout v1

## Decision

**NO CANDIDATE.** The fixed strategy was profitable on Development but failed
the predeclared Validation return, profit-factor, trade-count, and
profit-concentration gates. No parameter was changed after observing the
result, and no v2 search was started.

## Fixed hypothesis and execution

Dataset: Bybit Spot BTCUSDT 1h,
`[2023-11-01T00:00:00Z, 2026-11-01T00:00:00Z)`, 26,304 valid candles.

For decision candle `t`:

- the baseline is the 120 candles `t-144..t-25`;
- compression is the 24 candles `t-24..t-1`;
- compression requires mean ATR% to be no more than 70% of baseline ATR%;
- BUY requires a new close above the compression high, a bullish candle,
  current ATR% at least 1.25 times compression ATR%, and RVOL20 at least 1.25;
- SELL is a new close below the prior 12-candle low;
- an independent 3% protective stop is anchored to the actual BUY fill;
- signals execute at the next hourly open;
- initial cash is 1,000 USDT, fee is 0.1% per fill, and fixed adverse
  slippage is 0.05% per fill.

The first Wilder ATR14 value exists at index 13. With the strategy's 158-row
minimum history, the baseline occupies indices 13..132, compression occupies
133..156, and the current candle is index 157. Both historical windows are
therefore complete and strictly precede the signal candle.

Candidate gates were fixed before the run:

- positive net return on Development and Validation;
- profit factor above 1 on both periods;
- at least 30 completed trades on both periods;
- maximum close-equity drawdown no worse than -25%;
- largest winning trade no greater than 25% of gross profit.

## Results

| Period | Net return | Final equity | Max DD | PF | Trades | Win rate | Fees | Slippage | Largest win share | Buy and Hold |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Full range | +14.0389% | 1,140.3888 | -16.9451% | 1.3012 | 42 | 38.0952% | 98.74 | 49.37 | 25.4405% | +123.4733% |
| Development | +27.6650% | 1,276.6502 | -16.9451% | 2.1992 | 21 | 47.6190% | 48.48 | 24.24 | 30.4110% | +232.0544% |
| Validation | -4.8855% | 951.1450 | -10.1836% | 0.5284 | 11 | 18.1818% | 20.92 | 10.46 | 77.8949% | -40.6378% |
| Studied Test | -6.0851% | 939.1486 | -8.1269% | 0.2834 | 10 | 40.0000% | 19.40 | 9.70 | 45.1474% | +13.0328% |

Studied Test is descriptive only. It was previously observed and did not
participate in selection or parameter changes.

### Existing BreakoutStrategy reference

The existing strategy was evaluated with unchanged signal parameters and the
same fee, slippage, all-in sizing, and 3% protective-stop wrapper.

| Period | Net return | Max DD | PF | Trades |
|---|---:|---:|---:|---:|
| Full range | -22.5037% | -47.4301% | 0.9041 | 147 |
| Development | +8.4840% | -32.4086% | 1.0557 | 92 |
| Validation | -20.5785% | -31.7171% | 0.5103 | 29 |
| Studied Test | -10.0550% | -21.0703% | 0.7632 | 26 |

The compression variant was materially more selective and had a lower
drawdown, but it still failed the absolute Validation gates.

## Signal diagnostics

Across the full range, 26,147 rows had complete strategy diagnostics. The
strategy emitted 61 BUY and 1,076 position-agnostic SELL market events. Many
SELL events occurred while the long-only portfolio was flat; this does not
create short exposure.

| Condition | Full-range independent passes | Development | Validation |
|---|---:|---:|---:|
| Compression | 4,505 | 2,725 | 859 |
| Upper breakout | 1,101 | 675 | 219 |
| New breakout event | 808 | 504 | 151 |
| Bullish candle | 13,194 | 7,943 | 2,616 |
| ATR expansion | 3,121 | 1,900 | 597 |
| Relative volume | 6,780 | 4,057 | 1,376 |

Sequential first blockers over the full range were compression 21,642 times,
upper breakout 4,246 times, ATR expansion 130 times, repeated breakout 64
times, and relative volume 4 times. A bullish candle was never the first
blocker after an upper close breakout in this sample.

Compression and ATR expansion are compatible rather than contradictory:

- both passed: 981 rows;
- compression only: 3,524 rows;
- expansion only: 2,140 rows;
- neither passed: 19,502 rows.

Only 61 rows subsequently passed every BUY condition. Compression is the
dominant first filter, while lack of a confirmed upper breakout is the main
blocker among compressed observations.

## Costs and exits

| Period | Return with costs | Zero-cost return | PF with/without costs |
|---|---:|---:|---:|
| Full range | +14.0389% | +29.2230% | 1.3012 / 1.7260 |
| Development | +27.6650% | +35.8308% | 2.1992 / 2.7928 |
| Validation | -4.8855% | -1.6944% | 0.5284 / 0.7907 |
| Studied Test | -6.0851% | -3.2250% | 0.2834 / 0.5355 |

Costs materially reduced performance, but Validation remained negative with
zero fee and zero slippage. They therefore worsened, but did not create, the
Validation failure.

Full-range exits comprised 40 strategy SELL trades with +215.83 USDT net PnL
and two protective intrabar stops with -75.45 USDT net PnL.

## Gate decision

Development failed:

- only 21 completed trades instead of 30;
- largest winner was 30.4110% of gross profit instead of at most 25%.

Validation failed:

- net return was negative;
- profit factor was below 1;
- only 11 completed trades instead of 30;
- largest winner was 77.8949% of gross profit.

The low Validation drawdown passed, but cannot override the other failures.
The final status is **NO CANDIDATE**.

## Reproduction

```bash
python scripts/research_volatility_compression_breakout.py \
  --database data/market_data.db
```

The script first runs the existing SQLite quality check. It does not access
the network, change market data, tune parameters, or write to the database.

## Limitations

- The study covers one asset, one exchange, and one hourly dataset.
- Validation is chronological but comes from a market history already used by
  other CryptoLab research; it is not a genuinely new external sample.
- The Studied Test is not an independent holdout.
- Hourly OHLC data cannot identify the exact intrabar path or order-book fill.
- Fixed slippage, fees, and the 3% stop are modeling assumptions.
- The result rejects only this exact v1 specification; it neither establishes
  nor disproves a general volatility-compression effect.
