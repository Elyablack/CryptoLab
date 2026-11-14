# Signal research and event study

The event study asks whether the existing strategy decisions precede favorable
price movement. It deliberately excludes portfolio cash, position sizing, FIFO
lots, and trade lifecycle rules. Its output must not be interpreted as a
backtest or as executable short performance.

## Timeline

For a decision based on candle `t`:

```text
candle t close / StrategyDecision available
              ↓
candle t+1 open = event entry price
              ↓
N complete hourly candles
              ↓
close of candle t+N = forward-horizon price
```

The entry is therefore never the signal candle's historical open. A 1h event
uses the next candle's open-to-close return. A 6h event uses the next open and
the close after six complete hourly candles. MAE and MFE use the highs and lows
inside exactly that future window. They are outcome diagnostics and never feed
signal generation.

Ordinary market return is always `exit / entry - 1`. BUY directional return is
the same value. SELL directional return is its negative, so a decline is
positive. This does not model a short order: in CryptoLab Spot semantics SELL
still means reduce or exit existing long exposure.

## Chronological boundaries

The study reuses the optimization split: 60% Train, 20% Validation, and 20%
studied Test. A signal is assigned to the period containing its signal candle.
Its next-open entry and entire future window must also fit in that period.
Otherwise the event is excluded for that horizon. Earlier rows may warm up
indicators, while later periods cannot supply outcomes to an earlier period.

The `[2026-03-26, 2026-11-01)` BTCUSDT period has already been examined by the
optimizer and is explicitly labeled `studied_test`. It is useful descriptive
evidence but not a fresh holdout. Confirmation requires new candles after the
documented dataset end or another market chosen before examining its results.

## Controls and uncertainty

For each strategy, side, period, and horizon, controls are selected from entry
moments which:

- have the same complete-period constraint;
- are not an actionable entry of that strategy;
- share the signal's trend and ATR regime known on the preceding candle;
- are sampled without replacement using a stable hash and fixed random seed.

Signal and control directional returns are paired. Signal windows that overlap
in time are merged into temporal clusters. The report resamples whole clusters
and recomputes the event-weighted mean, rather than resampling individual
hourly events, to produce an approximate 95% interval for the matched edge. A
minimum of 20 matched observations and 20 independent clusters is required by
default. Otherwise the conclusion is
`inconclusive_insufficient_independent_events`.

This is a conservative dependency adjustment, not a formal causal model.
Controls may still differ in unobserved market conditions. No multiple-testing
correction is applied across strategies, sides, horizons, periods, or regime
tables.

## Predeclared regimes

All labels use values available at the signal close:

- RSI: `oversold <= 30`, `neutral`, `overbought >= 70`;
- ATR/close: `low < 0.5%`, `normal < 1.5%`, otherwise `high`;
- RVOL: `low < 0.8`, `normal < 1.5`, otherwise `high`;
- price versus SMA50: `above` or `below`;
- trend: `bullish` for `close > SMA20 > SMA50`, `bearish` for the reverse,
  otherwise `mixed`;
- trend strength from `|SMA20-SMA50| / close`: `weak < 0.5%`,
  `moderate < 2%`, otherwise `strong`.

Regime results are one-dimensional descriptive breakdowns. CryptoLab does not
combine them into automatically tuned filters or create new strategies.

## Costs

The cost-exceedance statistic compares directional returns with the exact
round-trip threshold implied by configured fixed adverse slippage and per-fill
fees:

```text
((1 + fee) × (1 + slippage))
 / ((1 - fee) × (1 - slippage))) - 1
```

With defaults this is approximately 0.30045%. It is an estimated hurdle, not
account-specific Bybit pricing, and SELL directional returns still do not
represent short PnL.

## Reproduction

```bash
cryptolab quality --exchange bybit --symbol BTCUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 --database data/market_data.db

cryptolab signals-research \
  --exchange bybit --symbol BTCUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 \
  --database data/market_data.db --strategy all \
  --random-seed 20261114 --bootstrap-samples 1000 \
  --minimum-independent-events 20 \
  --output-json reports/signal-study.json \
  --output-csv reports/signal-study.csv
```

The JSON contains exact Decimal strings, auditable per-signal entry/exit
timestamps and outcomes, empirical quantiles, every horizon, matched-control
comparisons, confidence intervals, and all regime rows. CSV contains
`signal_count`, `event_observation`, `horizon_summary`, and `regime_summary`
row types.
