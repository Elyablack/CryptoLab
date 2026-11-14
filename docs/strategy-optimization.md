# Strategy optimization research

CryptoLab optimization is a deterministic research workflow, not strategy
autotuning and not evidence of future profitability. It reuses the existing
`FeatureBuilder`, strategy classes, `BacktestEngine`, Decimal accounting, fee,
slippage, and next-candle-open rules.

## Reproducible workflow

For Bybit Spot BTCUSDT hourly data:

```bash
cryptolab collect --exchange bybit --symbol BTCUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 --database data/market_data.db

cryptolab quality --exchange bybit --symbol BTCUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 --database data/market_data.db

cryptolab optimize --exchange bybit --symbol BTCUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 --database data/market_data.db \
  --strategy all --min-trades 3 --max-combinations 100 \
  --walk-forward-folds 3 \
  --output-json reports/btcusdt-optimization.json \
  --output-csv reports/btcusdt-grid.csv
```

`optimize` performs no HTTP requests. Collection must finish first, and exact
quality validation must report zero missing, unexpected, duplicate, and invalid
candles.

## Split and boundary semantics

The default chronological split is 60% train, 20% validation, and 20% test.
All boundaries are half-open and aligned to candle opens. Earlier rows are
visible to indicators as warm-up history, but a period gate emits only HOLD
before the evaluation start. Every period starts with fresh cash. Consequently:

- train positions do not carry into validation;
- validation positions do not carry into test;
- open positions are marked at the period's last close without a fabricated
  SELL or exit fee;
- a last-candle signal has no next candle and remains unexecuted;
- later rows are filtered by `as_of_time` and cannot influence earlier signals.

This boundary policy measures independent period behavior. It is not a
continuous portfolio simulation across the full dataset.

## Selection

Every candidate is evaluated on train and validation. Eligibility requires the
configured minimum number of completed trades in both periods. Eligible
configurations are ranked by:

```text
validation return
+ 0.5 × validation maximum drawdown
- 0.25 × |train return - validation return|
```

Maximum drawdown is non-positive, so its term penalizes deeper losses. The
score is intentionally simple and includes risk and stability rather than
return alone. The holdout test period is not read during ranking. After
selection, the selected configuration and the unchanged baseline are evaluated
once on test. If no candidate is eligible, no optimized configuration is
selected.

Optional walk-forward folds use expanding training windows and consecutive
validation slices inside the pre-test region. They diagnose an already selected
configuration and do not alter selection.

## Metrics and interpretation

Reports contain return, final equity, total/realized/unrealized PnL, maximum
drawdown, fills, completed trades, win rate, profit factor, fees, exposure,
skipped BUY/SELL counts, and open-position state. Buy-and-hold uses the same
entry fee and adverse slippage; cash-only stays at 1000 USDT.

Profit factor is undefined (`null`/`N/A`) when there are no losing completed
trades. Inactive strategies are not treated as successful. Fewer than five
validation or holdout trades produces a warning. Even a favorable holdout is
only a reason for further research across other assets and regimes.

## Custom grids

The built-in grids contain 64 total combinations and include unchanged default
parameters. A custom JSON object can specify any subset of constructor fields;
other fields retain defaults:

```json
{
  "trend_following": {
    "buy_rsi_min": ["45", "50"],
    "buy_rsi_max": ["65", "70"],
    "sell_rsi_max": ["45", "50"],
    "minimum_relative_volume": ["0.8", "1"],
    "exit_policy": ["confirmed", "crossover_only"]
  }
}
```

Values are validated by the actual strategy dataclasses. If the Cartesian
product exceeds `--max-combinations`, the run fails rather than truncating or
sampling silently.
