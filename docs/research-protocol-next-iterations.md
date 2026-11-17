# CryptoLab Research Protocol — Next Iterations

Status: **draft for approval**  
Supersedes no existing strategy or deployment configuration.  
Volatility Compression Breakout v1 closure: **NO_CANDIDATE**.

## 1. Purpose and boundaries

This protocol governs future hypothesis research after Volatility Compression
Breakout v1. It is intended to limit repeated testing, make negative and
inconclusive results visible, and reserve genuinely unseen data for final
confirmation.

It does not authorize:

- a Volatility Compression Breakout v2;
- modification of v1 parameters, exits, candidate gates, or paper deployment;
- live trading or authenticated exchange execution;
- selection of a new threshold or horizon from the v1 diagnostic tables.

## 2. Closure record for Volatility Compression Breakout v1

### 2.1 Observed facts

- The fixed v1 configuration returned +27.6650% on Development and -4.8855%
  on Validation after fees and modeled slippage.
- Development contained 21 completed trades and Validation contained 11,
  below the fixed minimum of 30 trades on each period.
- The largest winning trade contributed 30.4110% of Development gross profit
  and 77.8949% of Validation gross profit, both above the 25% limit.
- Validation profit factor was 0.5284 and remained below 1 in the zero-cost
  diagnostic. Validation zero-cost return remained negative at -1.6944%.
- Maximum close-equity drawdown passed the -25% limit on both periods.
- The strategy was exposed at 4.8853% of Development candle closes and 1.2261%
  of Validation candle closes.
- New 24-hour upper breakouts following compression had higher descriptive
  mean forward returns than the broader new-breakout population at each fixed
  6, 12, 24, and 48-hour horizon in both Development and Validation.
- The event groups were nested and many forward windows overlapped. The event
  summaries were not executable portfolio returns or independent samples.
- Among compressed new breakouts, ATR expansion passed 36/110 Development
  events and 12/38 Validation events. RVOL passed 97/110 and 33/38. Both
  passed 33 and 12 events respectively.
- Only two full-range completed trades exited through the fixed protective
  stop. The other 40 completed trades used the strategy SELL.

### 2.2 Explanations not established by the evidence

The following must remain hypotheses rather than conclusions:

- Compression itself caused the descriptive improvement in forward returns.
  Time selection, volatility regime, trend, or another correlated variable
  could explain the difference.
- The 0.70 compression threshold is optimal or stable.
- ATR expansion improves economic performance. It was the more selective
  filter, but selectivity is not evidence of usefulness.
- RVOL improves economic performance. It rejected few compressed breakouts,
  and no controlled ablation was performed.
- A 48-hour exit, a different range exit, or another stop would improve the
  executable strategy. Diagnostic horizons did not model portfolio exits.
- The 3% stop reduced risk effectively. Two observed stop exits are
  insufficient evidence.
- The behavior generalizes beyond BTCUSDT, Bybit Spot, 1h, or the reused
  2023-2026 history.

The v1 result is therefore closed as **NO_CANDIDATE**, not as evidence that
volatility compression can never contain useful information.

## 3. Data-use map

### 3.1 Primary BTCUSDT timeline

| Label | UTC half-open range | Candles | Prior use | Independence status |
|---|---|---:|---|---|
| Development | `[2023-11-01 00:00, 2025-08-19 14:00)` | 15,782 | Existing strategy optimization, signal event study, Trend Pullback development and exit study, BTC/ETH Rotation development, VCB v1 development and diagnostics | Fully studied; development data only |
| Validation | `[2025-08-19 14:00, 2026-03-26 18:00)` | 5,260 | Existing optimization ranking, strategy and exit validation, Rotation candidate decision, signal event study, VCB v1 candidate decision and diagnostics | Reused many times; not an independent validation set |
| Studied Test | `[2026-03-26 18:00, 2026-11-01 00:00)` | 5,262 | Existing optimized-strategy evaluation, Trend Pullback reporting, Rotation reporting, signal event study, VCB v1 descriptive report; October 2026 baseline is a subset | Already observed; not a holdout |

Additional overlapping research includes the 18-month Trend Following and
trailing-stop reports over `[2025-05-01, 2026-11-01)`. Consequently, renaming
or repartitioning any part of the table above does not restore independence.

ETHUSDT over the same full range was used in the BTC/ETH Relative Momentum
Rotation study. It is not a fresh confirmation dataset for that hypothesis.
Using another symbol after observing BTC results may provide robustness
evidence, but it is not automatically an independent BTC holdout.

### 3.2 Current stored-data boundary

The local SQLite inventory currently contains exactly 26,304 hourly Bybit
Spot candles for both BTCUSDT and ETHUSDT, ending with the candle opened at
`2026-10-31T23:00:00Z`. The half-open stored range ends at
`2026-11-01T00:00:00Z`.

There are therefore **no post-2026-11-01 BTCUSDT or ETHUSDT candles currently
stored**. This inventory fact does not make `2026-11-01T00:00:00Z` an
automatic prospective boundary. No existing segment can be honestly presented
as a new independent holdout.

## 4. Proposed evaluation sequence

### 4.1 Historical development

Historical work may continue only as development and robustness analysis:

1. Register one economic hypothesis, its exact data inputs, parameters,
   execution model, risk model, metrics, and failure modes before execution.
2. Use expanding or sequential time-ordered folds. Every decision in a fold
   may use only candles available before its signal timestamp.
3. Keep earlier candles for indicator warm-up while starting each evaluation
   portfolio with fresh capital at the declared boundary.
4. Report every fold, including inactive, negative, and risk-violating folds.
5. Treat all results from the current 2023-2026 history as development
   evidence, even if a fold is called “validation” internally.

Sequential folds can reveal regime instability and implementation defects.
They cannot create a new independent sample from already observed history.

### 4.2 Internal confirmation on studied history

One configuration selected solely under a registered Development rule may be
run once on a later chronological block of the studied history. Its status
must be labelled **internal confirmation on reused data**, not holdout proof.

Failure ends the hypothesis family under its registered experiment budget.
Success permits `PENDING_FRESH_DATA` only when every predeclared internal
return, risk, evidence-count, concentration, and data-quality gate has passed
and the exact strategy version and configuration have been frozen. The status
cannot replace `REJECTED_NEGATIVE`, `REJECTED_RISK`, or `INSUFFICIENT_DATA`.

### 4.3 Final confirmation on genuinely unseen data

Final confirmation requires candles whose outcomes and relevant market
information were unavailable to the research process when the final
hypothesis, rules, and configuration were registered and frozen. Each future
experiment must declare its own prospective boundary. That boundary must be no
earlier than the registration timestamp and may need to be later when candles,
market summaries, charts, reports, news, or other outcome-relevant information
have already been reviewed.

The experiment journal must record both machine-readable data access and
material human exposure to market information. A candle is not fresh merely
because it was not previously passed through CryptoLab code.

Before collecting or inspecting outcome metrics, register:

- the immutable strategy and code revision;
- exchange, market type, symbol, interval, and start boundary;
- minimum event and completed-trade requirements;
- the first calendar review date, the outcome if evidence is insufficient,
  and any one permitted extension with its final deadline;
- candidate return, profit-factor, drawdown, concentration, and data-quality
  gates;
- treatment of open positions at the observation boundary;
- rules for operational data corrections.

During accumulation, normal collection and data-quality monitoring are
allowed. Outcome-based threshold changes, alternative exits, and ranking of
subperiods are not. If the strategy changes, accumulated data becomes
development evidence for the changed variant and a new future confirmation
boundary is required.

Because no later candles are currently stored, the immediate alternative after
a future experiment is fully registered is **prospective offline
observation**:

- leave paper deployment unchanged;
- collect future public candles through the existing collector;
- run a frozen research strategy offline or in a non-deploying shadow report;
- record signals, data-quality status, and event counts;
- defer candidate metrics until the predeclared maturity condition or review
  date is reached.

Prospective observation is not a promise of eventual sufficiency or success.
The registration time, not the end of the current database, establishes the
earliest possible boundary.

## 5. Predeclared experiment budget

Each future hypothesis family should have this maximum budget:

1. **One baseline specification.** It includes one entry definition, one exit
   definition, one risk configuration, and one cost model.
2. **At most two predeclared alternatives**, for no more than three total
   executable configurations across the entire hypothesis family. Renaming a
   strategy, creating v2/v3, moving it to another module, or making a small
   threshold, filter, exit, stop, or sizing change does not reset the budget.
   Alternatives must be specified before any result from the family is read.
3. **One Development execution per registered configuration.** Reruns with
   identical code and data for determinism do not consume additional budget.
4. **One internal-confirmation execution** for the single configuration chosen
   by the registered Development selection rule.
5. **One fresh-data confirmation execution** after the maturity condition.
6. Zero post-result rescue variants on the same confirmation period.

Benchmarks, invariant checks, and exact reruns are not strategy variants.
However, any alternative threshold, filter, holding period, exit, stop, sizing
rule, event horizon used for selection, or conditional subperiod analysis
counts as an experiment and must appear in the journal.

Exploratory diagnostics may be performed after a rejection, but they must be
labelled diagnostic, cannot change the closed decision, and cannot select an
unregistered successor strategy.

All observed results consume the family budget and remain in the journal,
including results later relabelled exploratory, configurations subsequently
renamed, and failed v2 implementations. A new family identifier requires a
genuinely distinct economic hypothesis, not a new name for the same signal.

### 5.1 Reproduction versus a new research execution

A **technical reproduction** repeats the same immutable code revision,
configuration, dataset identity, date boundaries, and execution assumptions.
It is permitted for determinism and does not consume another configuration or
research-run slot. Its output must match the registered tolerance, and the
journal should link it to the original record.

A run is a **new research execution** if any signal rule, threshold, feature,
exit, stop, sizing rule, cost assumption, dataset boundary, selection rule, or
material implementation behavior changes. It requires a new journal record and
consumes the applicable family budget. Once results from a data interval have
been inspected, that interval is not fresh confirmation data for the changed
variant, even if the change is small or described as a bug-independent v2.

A correctness defect is handled separately: preserve the invalid result,
record `INVALID_IMPLEMENTATION`, correct only the defect, and reproduce the
original registered specification. If the correction changes the intended
financial semantics rather than merely restoring them, it is a new research
execution.

## 6. Append-only experiment journal

Every attempted configuration should add one immutable journal record with:

- experiment and hypothesis-family identifiers;
- registration and execution timestamps;
- research question and economic rationale;
- code revision and configuration hash;
- exact dataset identity, exchange, symbol, interval, range, and candle count;
- data-quality report and known prior uses of the period;
- material market data, charts, reports, news, and outcome summaries reviewed
  by researchers before registration;
- entry, exit, sizing, fee, slippage, and risk assumptions;
- all parameters and permitted alternatives;
- Development selection rule and final candidate gates;
- results for all declared periods and benchmarks;
- number of signals, fills, completed trades, open positions, and skipped
  decisions;
- status and explicit rejection reasons;
- deviations, implementation defects, corrected reruns, and links to reports.

Records must not be deleted when a hypothesis fails. A correction should
append a superseding record while retaining the invalid original and its
reason.

## 7. Status and stopping rules

Research outcomes should use distinct statuses rather than one generic fail:

| Status | Meaning | Required action |
|---|---|---|
| `REJECTED_NEGATIVE` | Return or profit factor fails a registered economic gate | Stop the family; do not rescue it on the same data |
| `REJECTED_RISK` | Drawdown, concentration, exposure, or another risk limit fails | Stop even if return is positive |
| `INSUFFICIENT_DATA` | Signal/trade count or calendar maturity is below the registered minimum | Do not weaken the gate; wait for new data or close inconclusive |
| `INVALID_DATA` | Missing, misaligned, unclosed, or invalid candles | Do not interpret metrics; repair data non-destructively and rerun the same specification |
| `INVALID_IMPLEMENTATION` | Look-ahead, accounting, execution, or reproducibility defect | Preserve the invalid record, correct the defect, and rerun the unchanged hypothesis |
| `BUDGET_EXHAUSTED` | The predeclared configuration/run budget is consumed | Stop the family |
| `PENDING_FRESH_DATA` | Every predeclared internal gate passed, the strategy version is frozen, and no independent confirmation exists | Observe prospectively from an experiment-specific registered boundary; never use this status to replace a negative, risk, or evidence-count failure |
| `CONFIRMED_CANDIDATE` | All registered gates pass once on mature, genuinely unseen data | Eligible for a separate deployment review, not automatic trading |

Multiple rejection reasons may apply simultaneously. Return success never
overrides a risk violation, and a small sample never becomes sufficient merely
because its result is positive.

Volatility Compression Breakout v1 closes with:

- Development: `INSUFFICIENT_DATA` and `REJECTED_RISK` due to trade count and
  profit concentration;
- Validation: `REJECTED_NEGATIVE`, `INSUFFICIENT_DATA`, and `REJECTED_RISK`;
- overall: **NO_CANDIDATE**.

It is not eligible for `PENDING_FRESH_DATA` because its internal criteria did
not pass.

## 8. Feasibility of the 30-trade requirement

The fixed requirement remains at least 30 completed trades on each candidate
period. The observed v1 counts were:

| Period | Approximate duration | Completed trades | Approximate observed rate | Shortfall |
|---|---:|---:|---:|---:|
| Development | 657.6 days | 21 | 11.7 trades/year | 9 |
| Validation | 219.2 days | 11 | 18.3 trades/year | 19 |

A purely mechanical extrapolation of those historical average rates would
need approximately 2.57 years to observe 30 Development-rate trades and 1.64
years to observe 30 Validation-rate trades. These figures are planning
illustrations only, not forecasts:

- signal rates changed materially between the periods;
- compression regimes cluster in time;
- holding a position suppresses otherwise valid BUY events;
- future volatility and trend regimes may reduce or increase the rate;
- completed trades are not guaranteed to be statistically independent.

Combining 21 Development and 11 Validation trades into 32 does not satisfy a
criterion requiring 30 on each period. Repartitioning the same 32 studied
trades also does not provide new evidence.

The criterion should not be weakened. Acceptable outcomes are:

1. accumulate genuinely new data within the predeclared observation window
   until the maturity rule is met;
2. reach the predeclared calendar review date with fewer than 30 trades and
   record `INSUFFICIENT_DATA`, unless one specific extension and its final
   deadline were registered before observation began;
3. conclude that the strategy is too rare to evaluate within the project's
   practical research horizon.

Extending the calendar range increases opportunity but does not guarantee an
adequate sample. Open-ended continuation until a positive result or sufficient
trade count is prohibited. At the final registered deadline, fewer than 30
completed trades produces `INSUFFICIENT_DATA` and closes the confirmation run.
An extension cannot be invented after the first review date or after viewing
performance.

## 9. Approval decisions required before the next iteration

Before another hypothesis is implemented, approve or amend:

1. the three-configuration maximum per hypothesis family;
2. the one-shot internal and fresh-data confirmation rules;
3. the append-only journal fields and status taxonomy;
4. the rule for choosing an experiment-specific prospective boundary no
   earlier than final registration and later than any already reviewed market
   information;
5. the maturity rule combining a minimum of 30 completed trades with a fixed
   calendar review date, explicit `INSUFFICIENT_DATA` outcome, and at most one
   pre-registered extension with a final deadline;
6. the rule that studied-history success can produce only
   `PENDING_FRESH_DATA` after all internal gates pass and the strategy is
   frozen, never final confirmation;
7. the rule that reproduction is exact and non-consuming, while any changed
   research behavior creates a new journal record, consumes family budget, and
   invalidates already viewed data as fresh evidence for that variant.

No next hypothesis is selected by this document.
