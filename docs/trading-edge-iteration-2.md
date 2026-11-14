# Trading Edge Discovery — Iteration 2

## Decision

**NO CANDIDATE.** The fixed BTC/ETH Relative Momentum Rotation rule failed
the predeclared Validation return, drawdown, and profit-concentration gates.
Positive full-range and Studied Test results do not override that result.

## Data and fixed protocol

Dataset: Bybit Spot BTCUSDT and ETHUSDT 1h,
`[2023-11-01T00:00:00Z, 2026-11-01T00:00:00Z)`. Each instrument contains
26,304 valid, aligned candles with no missing, unexpected, duplicate, or
invalid rows. Collection-time conflicts are reported by `collect` but are not
stored as a durable conflict log, so `quality` cannot retrospectively audit
that counter.

Rules were fixed before inspecting the ETH results:

- review every seven days on a schedule anchored to dataset start;
- compare BTC and ETH close momentum over 30 calendar days;
- hold the asset with the larger positive momentum, otherwise USDT;
- keep the current asset on an exact positive tie, otherwise use BTC as the
  deterministic tie-break;
- do not trade when the target is unchanged;
- execute at the next hourly open, selling before buying when rotating;
- use long-only Spot, one active asset, 1,000 USDT initial cash, 0.1% fee per
  fill, and 0.05% adverse slippage per fill;
- mark an open final position at the last close without a hypothetical exit.

The primary scenario allocates 100% of available quote capital. A fixed 50%
allocation is only a risk diagnostic and cannot replace the primary scenario.
Development, Validation, and Studied Test start with fresh portfolios. Earlier
candles are retained only for momentum lookback. Studied Test was already
observed and was not used for selection.

## Results

### Primary 100% rotation and benchmarks

| Period | Model | Net return | CAGR | Max DD | PF | Episodes | Fees | Slippage | Exposure |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Full | Rotation | +159.9995% | +37.4968% | -43.9114% | 1.8813 | 31 | 96.35 | 48.18 | 59.3978% |
| Full | BTC Buy-and-Hold | +123.4733% | +30.7320% | -53.7338% | N/A | 0 | 1.00 | 0.50 | 100% |
| Full | ETH Buy-and-Hold | +24.0875% | +7.4572% | -69.1512% | N/A | 0 | 1.00 | 0.50 | 100% |
| Full | Equal-weight BTC/ETH | +73.7804% | +20.2215% | -59.4061% | N/A | 0 | 1.00 | 0.50 | 100% |
| Full | Cash | 0% | 0% | 0% | N/A | 0 | 0 | 0 | 0% |
| Development | Rotation | +146.3663% | +65.0065% | -35.2942% | 2.2671 | 21 | 56.34 | 28.17 | 61.3674% |
| Validation | Rotation | -32.4422% | -47.9828% | -42.1181% | 0.2238 | 6 | 11.12 | 5.56 | 35.0000% |
| Studied Test | Rotation | +56.2057% | +110.2232% | -13.2112% | 17.9507 | 4 | 10.19 | 5.10 | 76.6249% |

Validation references returned -40.6378% for BTC Buy-and-Hold, -52.3302% for
ETH Buy-and-Hold, and -46.4840% for equal-weight BTC/ETH. Rotation was less
negative with a smaller drawdown than these invested benchmarks, but it still
lost 32.4422% and failed the absolute gates. Cash returned 0%.

### Fixed 50% risk scenario

| Period | Net return | Max DD | PF | Episodes | Fees | Exposure |
|---|---:|---:|---:|---:|---:|---:|
| Full | +72.9271% | -25.0178% | 2.0214 | 31 | 39.82 | 59.3978% |
| Development | +64.0127% | -19.2990% | 2.3314 | 21 | 25.17 | 61.3674% |
| Validation | -17.1813% | -23.5486% | 0.2218 | 6 | 5.94 | 35.0000% |
| Studied Test | +27.1378% | -8.1202% | 19.2724 | 4 | 4.67 | 76.6249% |

Reducing allocation reduced loss and drawdown in Validation, but the scenario
remained negative and is not a substitute candidate.

## Predeclared criteria

| Criterion | Development | Validation |
|---|---:|---:|
| Positive net return | PASS | **FAIL** |
| Max drawdown no worse than -25% | **FAIL** | **FAIL** |
| Minimum weekly reviews | PASS: 90/30 | PASS: 31/10 |
| Minimum target changes | PASS: 29/5 | PASS: 12/5 |
| Largest winning episode <=25% gross profit | PASS: 24.6946% | **FAIL: 68.5446%** |

Rotation passed the relative Validation criterion: it did not underperform
equal-weight Buy-and-Hold in both return and drawdown. This does not compensate
for the absolute failures.

## Diagnosis

The full range contains 153 reviews, 45 target changes, and 17 direct
BTC-to-ETH or ETH-to-BTC rotations. Targets were USDT 60 times, BTC 40 times,
and ETH 53 times. Completed episodes attributed +416.03 USDT to BTC and
+1,183.97 USDT to ETH before open-position PnL.

Observed facts:

- Development captured strong trends but exceeded the -25% risk limit.
- Validation was a regime failure: six completed episodes, profit factor
  0.2238, and -42.1181% close-equity drawdown.
- The largest Validation winner was 68.5446% of gross profit.
- Full-range costs were 96.35 USDT in fees and 48.18 USDT in modeled adverse
  slippage. Validation profit factor far below one means costs alone do not
  explain the loss.
- Studied Test was strong but already observed and contains only four
  completed episodes. It is not independent confirmation.

These are sample associations, not proof of causation. No lookback, review
interval, or filter was changed after observing the outcome.

## Reproduction

```bash
cryptolab quality \
  --exchange bybit --symbol BTCUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 \
  --database data/market_data.db

cryptolab quality \
  --exchange bybit --symbol ETHUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 \
  --database data/market_data.db

python scripts/research_relative_momentum.py \
  --database data/market_data.db
```

The script checks SQLite quality, runs the fixed rule and benchmarks, and
checks portfolio invariants. It does not call an API or write the database.

## Limitations

- There is no independent holdout in this iteration.
- Fixed slippage and next-open fills are not an order-book simulation.
- Drawdown uses hourly close equity, not intrabar lows.
- Profit factor and concentration include completed episodes only; total
  return includes mark-to-market PnL for an open final position.
- Exchange quantity filters, minimum notional, taxes, and fee tiers are not
  modeled.
- This hypothesis should not be rescued by tuning on the same periods.
