# Volatility Compression Breakout v1 — Diagnostic Analysis

## Scope

This diagnostic preserves the accepted v1 strategy, parameters, execution
assumptions, and candidate gates. It reads only Bybit Spot BTCUSDT 1h candles
from Development and Validation:

- Development: `[2023-11-01T00:00:00Z, 2025-08-19T14:00:00Z)`;
- Validation: `[2025-08-19T14:00:00Z, 2026-03-26T18:00:00Z)`.

The input contains 21,042 complete candles. Studied Test was not loaded or
evaluated. The event-study horizons were fixed before calculation at 6, 12,
24, and 48 hours, measured from the next open after a signal.

## Portfolio behavior

| Period | Strategy | Cash | Buy and Hold | Time exposed | Mean hold | Median hold | Min / max hold |
|---|---:|---:|---:|---:|---:|---:|---:|
| Development | +27.6650% | 0% | +232.0544% | 4.8853% | 36.71h | 24h | 6h / 106h |
| Validation | -4.8855% | 0% | -40.6378% | 1.2261% | 23.45h | 22h | 3h / 74h |

Development beat Cash by 27.6650 percentage points but trailed Buy and Hold by
204.3894 points. Validation trailed Cash by 4.8855 points while losing 35.7523
points less than Buy and Hold. Avoiding most market exposure reduced downside
in Validation, but it did not produce a positive absolute return.

The substantially lower Validation exposure reflects fewer accepted entries,
not an open position carried across a period boundary. Each period starts with
a fresh 1,000 USDT portfolio.

## BUY events, fills, and completed trades

| Period | BUY events | Next-open eligible | BUY fills | Skipped while holding | No next candle | Other skips | Completed | Open at end |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Development | 33 | 33 | 21 | 12 | 0 | 0 | 21 | 0 |
| Validation | 12 | 12 | 11 | 1 | 0 | 0 | 11 | 0 |

The complete Development/Validation reconciliation is therefore:

```text
45 BUY events
- 13 BUY events while already holding BTC
= 32 executed entries
- 0 positions remaining open at a period end
= 32 completed trades
```

The accepted full-range v1 report contained 61 BUY events and 42 completed
trades. Of these, 45 events and 32 trades belong to the permitted Development
and Validation periods. The remaining previously reported 16 events and 10
trades belong to Studied Test. Their six-event difference is deliberately not
reanalysed here because doing so would violate the requested scope. It must not
be attributed to a particular skip reason by inference.

## Descriptive event study

These results describe subsequent market-price paths. They are not executable
strategy returns: they omit portfolio state, fees, slippage, protective stops,
and the strategy's actual exit timing.

`N` is the number of events with a complete forward window. `Clusters` are
connected groups of overlapping event windows. They make overlap visible but
are not asserted to be statistically independent observations.

### All new upper breakouts of the prior 24-hour range

| Period | Horizon | N | Clusters | Events in overlapping clusters | Mean | Median | Positive | Mean MFE | Mean MAE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Development | 6h | 504 | 368 | 231 | +0.0335% | -0.0713% | 47.22% | +1.0676% | -1.0165% |
| Development | 12h | 504 | 277 | 354 | +0.0848% | -0.0496% | 48.21% | +1.4772% | -1.3439% |
| Development | 24h | 504 | 210 | 426 | +0.0598% | -0.1154% | 47.22% | +1.9463% | -1.9058% |
| Development | 48h | 504 | 117 | 481 | +0.4489% | +0.1488% | 51.79% | +2.8741% | -2.6575% |
| Validation | 6h | 151 | 114 | 65 | -0.0601% | -0.1310% | 39.07% | +0.8337% | -0.8260% |
| Validation | 12h | 151 | 87 | 102 | -0.1918% | -0.2324% | 45.70% | +1.0772% | -1.1779% |
| Validation | 24h | 151 | 67 | 121 | -0.2379% | -0.3056% | 43.71% | +1.5022% | -1.6785% |
| Validation | 48h | 150 | 36 | 141 | -0.3139% | -0.1336% | 48.67% | +2.1291% | -2.4563% |

The broad breakout population had weak Development central tendency and
negative Validation mean and median returns at every fixed horizon.

### New upper breakouts after compression

| Period | Horizon | N | Clusters | Events in overlapping clusters | Mean | Median | Positive | Mean MFE | Mean MAE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Development | 6h | 110 | 83 | 47 | +0.1157% | +0.1592% | 57.27% | +0.8621% | -0.7809% |
| Development | 12h | 110 | 65 | 73 | +0.1475% | -0.0449% | 49.09% | +1.3561% | -1.0706% |
| Development | 24h | 110 | 53 | 90 | +0.3981% | -0.0605% | 50.00% | +2.0254% | -1.6857% |
| Development | 48h | 110 | 48 | 98 | +0.6166% | +0.2133% | 53.64% | +3.1204% | -2.6457% |
| Validation | 6h | 38 | 29 | 16 | +0.0996% | -0.0283% | 44.74% | +0.8344% | -0.6858% |
| Validation | 12h | 38 | 21 | 25 | -0.0598% | +0.1864% | 55.26% | +1.0860% | -1.0882% |
| Validation | 24h | 38 | 19 | 27 | +0.0829% | -0.0068% | 50.00% | +1.7069% | -1.5638% |
| Validation | 48h | 38 | 17 | 30 | +0.2087% | +0.7764% | 57.89% | +2.4978% | -2.0610% |

The compressed subset has somewhat better descriptive forward averages than
the broad breakout population, including in Validation. That difference does
not establish a causal compression effect: the groups are nested, market
regimes and event timing differ, and most longer windows overlap.

### Events passing every v1 BUY condition

| Period | Horizon | N | Clusters | Events in overlapping clusters | Mean | Median | Positive | Mean MFE | Mean MAE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Development | 6h | 33 | 26 | 12 | +0.2902% | +0.3495% | 66.67% | +1.0140% | -0.8170% |
| Development | 12h | 33 | 22 | 18 | +0.5099% | +0.4679% | 54.55% | +1.7533% | -1.1337% |
| Development | 24h | 33 | 21 | 20 | +0.4392% | +0.0424% | 51.52% | +2.1722% | -1.6150% |
| Development | 48h | 33 | 21 | 20 | +1.3665% | +0.5587% | 54.55% | +3.6031% | -2.8219% |
| Validation | 6h | 12 | 12 | 0 | +0.0306% | -0.1039% | 33.33% | +0.8766% | -0.7127% |
| Validation | 12h | 12 | 12 | 0 | -0.1603% | -0.2628% | 41.67% | +0.9896% | -0.9811% |
| Validation | 24h | 12 | 11 | 2 | -0.0833% | -0.3617% | 33.33% | +1.4363% | -1.2610% |
| Validation | 48h | 12 | 11 | 2 | +0.8730% | +1.1507% | 66.67% | +2.6394% | -1.7146% |

Only 12 Validation events passed every entry condition. Their 48-hour result
must not be selected as a preferred horizon after observation. The actual
portfolio closed Validation trades after a mean 23.45 hours, applied costs and
stops, and also skipped one BUY while already invested. Event outcomes and
backtest returns therefore answer different questions.

## ATR expansion and RVOL after compressed breakouts

### Order-independent joint counts

| Period | Compressed breakouts | ATR pass | RVOL pass | Both | ATR only | RVOL only | Neither | v1 BUY |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Development | 110 | 36 | 97 | 33 | 3 | 64 | 10 | 33 |
| Validation | 38 | 12 | 33 | 12 | 0 | 21 | 5 | 12 |

Independent failures, where each condition is assessed regardless of the
other condition:

| Period | ATR failures | RVOL failures |
|---|---:|---:|
| Development | 74 | 13 |
| Validation | 26 | 5 |

ATR expansion was more selective in this sample. This is a frequency result,
not evidence that ATR caused better or worse returns.

### Why sequential blockers depend on filter order

| Period | ATR first: ATR rejects | ATR first: subsequent RVOL rejects | RVOL first: RVOL rejects | RVOL first: subsequent ATR rejects |
|---|---:|---:|---:|---:|
| Development | 74 | 3 | 13 | 64 |
| Validation | 26 | 0 | 5 | 21 |

Calling the second sequential filter the “main blocker” would therefore be
misleading. The order-independent joint table is the appropriate description.

All compressed new upper breakouts in these two periods were bullish close
candles, so the bullish-candle condition caused no additional rejection in
this sample.

## What is observed, and what remains unknown

Observed:

- v1 used little capital exposure: 4.89% on Development and 1.23% on
  Validation;
- Validation deterioration coincided with fewer entries, shorter average
  holding, a much lower completed-trade win rate, and negative 12/24-hour
  forward outcomes for the fully confirmed entry events;
- repeated BUY events while already invested explain all Development and
  Validation differences between BUY events and completed trades;
- compression-conditioned events look descriptively better than the broad
  breakout set, but the fully confirmed Validation portfolio still lost;
- larger MFE than final forward return shows that favorable intraperiod moves
  often coexisted with adverse movement and giveback.

Unknown:

- whether compression itself caused the difference versus selection by time,
  volatility regime, or another correlated feature;
- whether a particular horizon has a stable advantage—the horizons were
  diagnostic, overlapping, and not selection candidates;
- whether the pattern generalizes beyond BTCUSDT, Bybit, 1h, or this reused
  historical sample;
- how exact intrabar paths, liquidity, and order-book execution would change
  MFE, MAE, stops, or fills;
- whether the remaining full-range BUY/trade difference in Studied Test has
  the same explanation, because that period was intentionally not reopened.

Validation has been reused across CryptoLab research and is not a fresh
holdout. The new strategy and existing `BreakoutStrategy` also differ in
range length, compression conditioning, ATR/RVOL thresholds, exit channel,
event policy interactions, and the diagnostic protective-stop wrapper.
Their performance difference cannot be attributed to compression alone.

No new strategy, v2 configuration, threshold selection, or optimization is
proposed by this report.

## Reproduction

```bash
python scripts/diagnose_volatility_compression_breakout.py \
  --database data/market_data.db
```
