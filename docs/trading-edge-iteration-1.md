# Trading Edge Discovery — Iteration 1

## Decision

**NO CANDIDATE.** None of the three predeclared Trend Pullback exit variants
passed the Development and Validation gates. Studied Test results were not used
for selection.

This conclusion applies only to the tested pullback signal and exit rules. It
does not establish that no BTCUSDT Spot edge can exist.

## Fixed protocol

Dataset: Bybit Spot BTCUSDT 1h,
`[2023-11-01T00:00:00Z, 2026-11-01T00:00:00Z)`, 26,304 valid candles.

Common assumptions:

- the Trend Pullback v1 entry rules are unchanged;
- initial cash is 1,000 USDT;
- fee is 0.1% per fill;
- adverse slippage is 0.05% per fill;
- the fixed protective stop is 3% from the actual entry execution price;
- strategy signals execute at the next candle open;
- open positions are not force-closed at a period or dataset boundary.

Exactly three variants were evaluated:

1. Baseline Trend Pullback v1 exit.
2. No strategy SELL; close at the open after 72 complete holding candles, or
   earlier through the protective stop.
3. SELL only when both close and SMA20 are below SMA50, or earlier through the
   protective stop.

No RSI, SMA, ATR, RVOL, stop, or time threshold was searched after results were
observed.

Candidate gates, declared before the run:

- positive net return on Development and Validation;
- profit factor above 1 on both periods;
- at least 30 completed trades on both periods;
- maximum drawdown no worse than -25%;
- largest winning trade no greater than 25% of gross profit.

## Baseline diagnosis

With normal costs the baseline returned -34.7955%, with profit factor 0.7670.
With zero fee and zero slippage it returned +26.5364%, with profit factor
1.1762. The execution path and 222 completed trades were unchanged.

Normal-cost attribution:

| Exit reason | Trades | Net PnL |
|---|---:|---:|
| Strategy SELL | 216 | -155.87 USDT |
| Protective intrabar stop | 6 | -192.09 USDT |

Fees were 366.51 USDT and modeled adverse-slippage impact was 183.26 USDT.
These fill-level values are not directly additive to the zero-cost final equity
because capital compounds and the stop anchor uses the actual execution price.

The protective stop fired only six times because the normal SELL usually
closed the position first. Before the 216 strategy exits, adverse excursion
averaged -0.8883%, had a -0.7828% median, and was -2.9850% at worst. Every one
of those trades stayed above the -3% stop before its exit open. Protective-stop
trades were held for 13.83 hours on average; all baseline trades averaged
21.84 hours.

## Full-range comparison

| Variant | Return | Max DD | PF | Trades | Win rate | Fees | Slippage | Avg trade | Avg hold |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Baseline | -34.7955% | -45.4891% | 0.7670 | 222 | 23.42% | 366.51 | 183.26 | -1.5674 | 21.84h |
| Time exit 72h | -31.1687% | -57.1608% | 0.8900 | 160 | 38.75% | 367.00 | 183.50 | -1.9480 | 56.98h |
| Long-term trend exit | -14.0849% | -43.9930% | 0.9203 | 160 | 28.13% | 329.63 | 164.82 | -0.8803 | 42.87h |

Cash returned 0%. Full-range Buy and Hold returned +123.4733%, with a
-53.7338% maximum close-equity drawdown and an open position at the end.

Exit attribution explains why the alternatives still failed:

- Time exit: 95 timed exits produced +2,099.13 USDT, while 65 protective exits
  produced -2,410.81 USDT.
- Long-term trend exit: 144 trend exits produced +427.30 USDT, while 16
  protective exits produced -568.15 USDT.

Longer holding allowed more trades to reach the fixed stop. The apparent
improvement in profitable exits did not compensate for those losses and costs.

## Chronological evidence

| Variant | Period | Return | Max DD | PF | Trades | Largest win share | Buy and Hold |
|---|---|---:|---:|---:|---:|---:|---:|
| Baseline | Development | -17.6651% | -31.1576% | 0.8286 | 139 | 15.57% | +232.0544% |
| Baseline | Validation | -5.4089% | -11.5716% | 0.7233 | 34 | 28.30% | -40.6378% |
| Baseline | Studied Test | -16.2772% | -24.6843% | 0.5803 | 49 | 65.17% | +13.0328% |
| Time exit 72h | Development | +19.7591% | -32.1371% | 1.1098 | 101 | 10.21% | +232.0544% |
| Time exit 72h | Validation | -16.8063% | -24.1955% | 0.6393 | 25 | 26.12% | -40.6378% |
| Time exit 72h | Studied Test | -30.5732% | -32.7536% | 0.4188 | 33 | 19.92% | +13.0328% |
| Long-term trend exit | Development | +11.3125% | -24.7242% | 1.0990 | 97 | 15.20% | +232.0544% |
| Long-term trend exit | Validation | -13.1906% | -16.3886% | 0.4477 | 27 | 32.07% | -40.6378% |
| Long-term trend exit | Studied Test | -11.0883% | -22.2175% | 0.7019 | 36 | 58.93% | +13.0328% |

Time exit failed the Development drawdown limit, then failed Validation return,
profit factor, trade count, and concentration. Long-term trend exit passed its
Development gates but failed Validation return, profit factor, trade count,
and concentration. No result from Studied Test changes this decision.

## Reproduction

```bash
python scripts/research_pullback_exits.py \
  --database data/market_data.db
```

The script performs the data-quality check, runs only the three fixed variants,
calculates benchmarks and costs, applies the declared gates, and prints
`SELECTED: NO CANDIDATE`. It does not access the network or write a report.

## Limitations

- This is one asset, one interval, and an already studied dataset.
- The zero-cost run is a counterfactual diagnostic, not a realizable result.
- The 72-hour exit and long-term trend rule were tested once; no adjacent
  values were searched.
- Hourly OHLC cannot reveal intrabar path or order-book liquidity.
- A new hypothesis requires a separately predeclared iteration and genuinely
  new confirmation data. These three exit variants should not be tuned further
  on the same Studied Test.
