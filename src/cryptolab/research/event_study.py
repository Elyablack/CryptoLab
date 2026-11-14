"""Deterministic event study for existing research strategy signals."""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from cryptolab.backtesting import BacktestConfig
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.intervals import get_interval_milliseconds
from cryptolab.market_data.markets import DEFAULT_EXCHANGE, SPOT_MARKET_TYPE
from cryptolab.market_data.models import Candle
from cryptolab.research.event_study_models import (
    RegimeSummary,
    ReturnDistribution,
    SignalCount,
    SignalEventObservation,
    SignalEventStudyReport,
    SignalHorizonSummary,
)
from cryptolab.research.validation import (
    ChronologicalSplit,
    ResearchPeriod,
    create_chronological_split,
)
from cryptolab.strategies import (
    BreakoutStrategy,
    MeanReversionStrategy,
    Signal,
    StrategyRunner,
    TrendFollowingStrategy,
)
from cryptolab.strategies.base import Strategy
from cryptolab.strategies.models import StrategyDecision


DEFAULT_HORIZONS_HOURS = (1, 6, 12, 24, 48, 72)


@dataclass(frozen=True)
class _SignalEvent:
    decision: StrategyDecision
    signal_index: int
    entry_index: int
    entry_time: int
    entry_price: Decimal
    period: ResearchPeriod
    regimes: tuple[tuple[str, str], ...]

    @property
    def coarse_regime(self) -> tuple[str, str]:
        labels = dict(self.regimes)
        return labels["trend"], labels["atr_percent"]


@dataclass(frozen=True)
class _Observation:
    event: _SignalEvent
    market_return: Decimal
    directional_return: Decimal
    mae: Decimal
    mfe: Decimal


class SignalEventStudyRunner:
    """Measure signal-following price behavior without portfolio accounting."""

    def run(
        self,
        symbol: str,
        interval: str,
        candles: Sequence[Candle],
        *,
        strategies: Sequence[Strategy] | None = None,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
        as_of_time: int | None = None,
        horizons_hours: Sequence[int] = DEFAULT_HORIZONS_HOURS,
        train_fraction: Decimal = Decimal("0.6"),
        validation_fraction: Decimal = Decimal("0.2"),
        fee_rate: Decimal = Decimal("0.001"),
        slippage_rate: Decimal = Decimal("0.0005"),
        random_seed: int = 20261114,
        bootstrap_samples: int = 1000,
        minimum_independent_events: int = 20,
    ) -> SignalEventStudyReport:
        if not candles:
            raise ValueError("At least one candle is required for event study")
        if type(random_seed) is not int:
            raise ValueError("random_seed must be an integer")
        if type(bootstrap_samples) is not int or bootstrap_samples <= 0:
            raise ValueError("bootstrap_samples must be a positive integer")
        if (
            type(minimum_independent_events) is not int
            or minimum_independent_events <= 0
        ):
            raise ValueError(
                "minimum_independent_events must be a positive integer"
            )
        config = BacktestConfig(
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
        )
        interval_milliseconds = get_interval_milliseconds(interval)
        horizon_steps = self._horizon_steps(
            horizons_hours,
            interval_milliseconds,
        )
        effective_as_of = (
            candles[-1].close_time + 1 if as_of_time is None else as_of_time
        )
        rows = tuple(FeatureBuilder().build(
            candles,
            interval=interval,
            as_of_time=effective_as_of,
        ))
        base_split = create_chronological_split(
            rows,
            train_fraction=train_fraction,
            validation_fraction=validation_fraction,
        )
        split = ChronologicalSplit(
            train=base_split.train,
            validation=base_split.validation,
            test=replace(base_split.test, name="studied_test"),
        )
        selected_strategies = tuple(strategies or (
            TrendFollowingStrategy(),
            MeanReversionStrategy(),
            BreakoutStrategy(),
        ))
        periods = (split.train, split.validation, split.test)
        events, counts = self._collect_events(
            symbol,
            interval,
            rows,
            selected_strategies,
            periods,
            effective_as_of,
        )
        estimated_cost = (
            (Decimal("1") + config.fee_rate)
            * (Decimal("1") + config.slippage_rate)
            / (
                (Decimal("1") - config.fee_rate)
                * (Decimal("1") - config.slippage_rate)
            )
            - 1
        )
        summaries: list[SignalHorizonSummary] = []
        observation_records: list[SignalEventObservation] = []
        regime_summaries: list[RegimeSummary] = []
        control_shortages = 0
        for strategy in selected_strategies:
            strategy_events = [
                event
                for event in events
                if event.decision.strategy_name == strategy.name
            ]
            excluded_by_period = {
                period.name: {
                    event.entry_index
                    for event in strategy_events
                    if event.period.name == period.name
                }
                for period in periods
            }
            for side in (Signal.BUY, Signal.SELL):
                for period in periods:
                    period_events = [
                        event
                        for event in strategy_events
                        if event.decision.signal is side
                        and event.period.name == period.name
                    ]
                    for horizon_hours, steps in horizon_steps:
                        observations = self._observe_events(
                            rows,
                            period_events,
                            period,
                            steps,
                            side,
                        )
                        observation_records.extend(
                            SignalEventObservation(
                                strategy_name=strategy.name,
                                side=side,
                                period=period.name,
                                signal_candle_open_time=(
                                    observation.event.decision.candle_open_time
                                ),
                                signal_available_time=(
                                    observation.event.decision.signal_available_time
                                ),
                                entry_open_time=observation.event.entry_time,
                                entry_price=observation.event.entry_price,
                                horizon_hours=horizon_hours,
                                exit_close_time=rows[
                                    observation.event.entry_index + steps - 1
                                ].close_time,
                                exit_price=rows[
                                    observation.event.entry_index + steps - 1
                                ].close,
                                market_return=observation.market_return,
                                directional_return=observation.directional_return,
                                mae=observation.mae,
                                mfe=observation.mfe,
                                exceeds_estimated_cost=(
                                    observation.directional_return > estimated_cost
                                ),
                                regimes=observation.event.regimes,
                            )
                            for observation in observations
                        )
                        controls, edges = self._matched_controls(
                            rows,
                            observations,
                            strategy.name,
                            side,
                            period,
                            steps,
                            horizon_hours,
                            excluded_by_period[period.name],
                            random_seed,
                        )
                        if len(controls) < len(observations):
                            control_shortages += 1
                        summary = self._summarize(
                            strategy.name,
                            side,
                            period.name,
                            horizon_hours,
                            observations,
                            controls,
                            edges,
                            estimated_cost,
                            random_seed,
                            bootstrap_samples,
                            minimum_independent_events,
                        )
                        summaries.append(summary)
                        regime_summaries.extend(self._regime_summaries(
                            strategy.name,
                            side,
                            period.name,
                            horizon_hours,
                            observations,
                        ))

        warnings = [
            (
                "The studied_test period was already used by prior strategy "
                "optimization and is not a new independent holdout."
            ),
            (
                "Forward returns describe directional signal quality, not "
                "portfolio PnL or executable short positions."
            ),
            (
                "Approximate 95% intervals bootstrap non-overlapping temporal "
                "clusters; overlapping hourly events are not treated as "
                "independent observations."
            ),
            (
                "Regime definitions and cost assumptions are fixed before "
                "examining results; no multiple-testing correction is applied."
            ),
        ]
        if control_shortages:
            warnings.append(
                f"{control_shortages} summary groups had fewer distinct matched "
                "control moments than signal observations."
            )
        return SignalEventStudyReport(
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            interval=interval,
            start_time=candles[0].open_time,
            end_time=candles[-1].close_time + 1,
            candle_count=len(candles),
            horizons_hours=tuple(hours for hours, _ in horizon_steps),
            split=split,
            random_seed=random_seed,
            bootstrap_samples=bootstrap_samples,
            minimum_independent_events=minimum_independent_events,
            fee_rate=config.fee_rate,
            slippage_rate=config.slippage_rate,
            estimated_round_trip_cost=estimated_cost,
            signal_counts=tuple(counts),
            observations=tuple(observation_records),
            summaries=tuple(summaries),
            regime_summaries=tuple(regime_summaries),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _horizon_steps(
        horizons_hours: Sequence[int],
        interval_milliseconds: int,
    ) -> tuple[tuple[int, int], ...]:
        if not horizons_hours:
            raise ValueError("At least one forward horizon is required")
        result = []
        seen = set()
        for hours in horizons_hours:
            if type(hours) is not int or hours <= 0:
                raise ValueError("Forward horizons must be positive integers")
            if hours in seen:
                raise ValueError("Forward horizons must be unique")
            milliseconds = hours * 3_600_000
            if milliseconds % interval_milliseconds != 0:
                raise ValueError(
                    f"{hours}h horizon is not divisible by the candle interval"
                )
            result.append((hours, milliseconds // interval_milliseconds))
            seen.add(hours)
        return tuple(result)

    def _collect_events(
        self,
        symbol: str,
        interval: str,
        rows: Sequence[FeatureRow],
        strategies: Sequence[Strategy],
        periods: Sequence[ResearchPeriod],
        as_of_time: int,
    ) -> tuple[list[_SignalEvent], list[SignalCount]]:
        index_by_open_time = {row.open_time: index for index, row in enumerate(rows)}
        events = []
        counts = []
        for strategy in strategies:
            decisions = StrategyRunner([strategy]).run(
                symbol,
                interval,
                rows,
                as_of_time=as_of_time,
            )
            for side in (Signal.BUY, Signal.SELL):
                for period in periods:
                    period_decisions = [
                        decision
                        for decision in decisions
                        if decision.signal is side
                        and period.start_time
                        <= decision.candle_open_time
                        < period.end_time
                    ]
                    eligible = 0
                    for decision in period_decisions:
                        signal_index = index_by_open_time[decision.candle_open_time]
                        entry_index = signal_index + 1
                        if entry_index >= period.end_index:
                            continue
                        entry_row = rows[entry_index]
                        if decision.signal_available_time != entry_row.open_time:
                            raise RuntimeError(
                                "Signal availability is not aligned to the next open"
                            )
                        events.append(_SignalEvent(
                            decision=decision,
                            signal_index=signal_index,
                            entry_index=entry_index,
                            entry_time=entry_row.open_time,
                            entry_price=entry_row.open,
                            period=period,
                            regimes=tuple(self._regimes(rows[signal_index]).items()),
                        ))
                        eligible += 1
                    counts.append(SignalCount(
                        strategy_name=strategy.name,
                        side=side,
                        period=period.name,
                        decisions=len(period_decisions),
                        entry_eligible=eligible,
                    ))
        return events, counts

    @staticmethod
    def _observe_events(
        rows: Sequence[FeatureRow],
        events: Sequence[_SignalEvent],
        period: ResearchPeriod,
        steps: int,
        side: Signal,
    ) -> list[_Observation]:
        result = []
        for event in events:
            if event.entry_index + steps > period.end_index:
                continue
            market_return, directional_return, mae, mfe = _forward_values(
                rows,
                event.entry_index,
                steps,
                side,
            )
            result.append(_Observation(
                event=event,
                market_return=market_return,
                directional_return=directional_return,
                mae=mae,
                mfe=mfe,
            ))
        return result

    def _matched_controls(
        self,
        rows: Sequence[FeatureRow],
        observations: Sequence[_Observation],
        strategy_name: str,
        side: Signal,
        period: ResearchPeriod,
        steps: int,
        horizon_hours: int,
        excluded_entries: set[int],
        seed: int,
    ) -> tuple[list[Decimal], list[tuple[int, Decimal]]]:
        by_regime: dict[tuple[str, str], list[_Observation]] = defaultdict(list)
        for observation in observations:
            by_regime[observation.event.coarse_regime].append(observation)

        candidates: dict[tuple[str, str], list[int]] = defaultdict(list)
        last_entry_exclusive = period.end_index - steps + 1
        for entry_index in range(period.start_index + 1, last_entry_exclusive):
            if entry_index in excluded_entries:
                continue
            labels = self._regimes(rows[entry_index - 1])
            candidates[(labels["trend"], labels["atr_percent"])].append(
                entry_index
            )

        controls = []
        edges = []
        for regime in sorted(by_regime):
            signal_group = sorted(
                by_regime[regime],
                key=lambda item: item.event.entry_time,
            )
            candidate_group = list(candidates.get(regime, ()))
            random.Random(_stable_seed(
                seed,
                strategy_name,
                side.value,
                period.name,
                str(horizon_hours),
                *regime,
            )).shuffle(candidate_group)
            for observation, entry_index in zip(
                signal_group,
                candidate_group,
                strict=False,
            ):
                _, control_directional, _, _ = _forward_values(
                    rows,
                    entry_index,
                    steps,
                    side,
                )
                controls.append(control_directional)
                edges.append((
                    observation.event.entry_time,
                    observation.directional_return - control_directional,
                ))
        return controls, edges

    @staticmethod
    def _summarize(
        strategy_name: str,
        side: Signal,
        period: str,
        horizon_hours: int,
        observations: Sequence[_Observation],
        controls: Sequence[Decimal],
        edges: Sequence[tuple[int, Decimal]],
        estimated_cost: Decimal,
        seed: int,
        bootstrap_samples: int,
        minimum_independent_events: int,
    ) -> SignalHorizonSummary:
        directional = [item.directional_return for item in observations]
        market = [item.market_return for item in observations]
        clusters = _overlap_clusters(
            edges,
            horizon_hours * 3_600_000,
        )
        edge_mean = _mean([value for _, value in edges])
        ci_lower, ci_upper = _bootstrap_interval(
            clusters,
            bootstrap_samples,
            _stable_seed(seed, strategy_name, side.value, period, str(horizon_hours)),
        )
        if (
            len(edges) < minimum_independent_events
            or len(clusters) < minimum_independent_events
        ):
            conclusion = "inconclusive_insufficient_independent_events"
        elif (
            ci_lower is not None
            and ci_lower > 0
            and _mean(directional) is not None
            and _mean(directional) > estimated_cost
        ):
            conclusion = "observed_positive_edge"
        elif ci_upper is not None and ci_upper < 0:
            conclusion = "observed_negative_edge"
        else:
            conclusion = "inconclusive"

        return SignalHorizonSummary(
            strategy_name=strategy_name,
            side=side,
            period=period,
            horizon_hours=horizon_hours,
            signal_count=len(observations),
            mean_market_return=_mean(market),
            mean_directional_return=_mean(directional),
            median_directional_return=_quantile(directional, Decimal("0.5")),
            favorable_share=_share(value > 0 for value in directional),
            sample_standard_deviation=_sample_standard_deviation(directional),
            distribution=ReturnDistribution(
                p05=_quantile(directional, Decimal("0.05")),
                p25=_quantile(directional, Decimal("0.25")),
                p50=_quantile(directional, Decimal("0.5")),
                p75=_quantile(directional, Decimal("0.75")),
                p95=_quantile(directional, Decimal("0.95")),
            ),
            minimum_directional_return=min(directional) if directional else None,
            maximum_directional_return=max(directional) if directional else None,
            cost_exceedance_share=_share(
                value > estimated_cost for value in directional
            ),
            mean_mae=_mean([item.mae for item in observations]),
            mean_mfe=_mean([item.mfe for item in observations]),
            control_count=len(controls),
            control_mean_directional_return=_mean(controls),
            control_median_directional_return=_quantile(
                controls,
                Decimal("0.5"),
            ),
            mean_matched_edge=edge_mean,
            edge_ci_lower=ci_lower,
            edge_ci_upper=ci_upper,
            effective_independent_clusters=len(clusters),
            conclusion=conclusion,
        )

    @staticmethod
    def _regime_summaries(
        strategy_name: str,
        side: Signal,
        period: str,
        horizon_hours: int,
        observations: Sequence[_Observation],
    ) -> list[RegimeSummary]:
        groups: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
        for observation in observations:
            for dimension, bucket in observation.event.regimes:
                groups[(dimension, bucket)].append(
                    observation.directional_return
                )
        return [
            RegimeSummary(
                strategy_name=strategy_name,
                side=side,
                period=period,
                horizon_hours=horizon_hours,
                dimension=dimension,
                bucket=bucket,
                observations=len(values),
                mean_directional_return=_mean(values) or Decimal("0"),
                favorable_share=_share(value > 0 for value in values)
                or Decimal("0"),
            )
            for (dimension, bucket), values in sorted(groups.items())
        ]

    @staticmethod
    def _regimes(row: FeatureRow) -> Mapping[str, str]:
        if row.rsi14 is None:
            rsi = "unavailable"
        elif row.rsi14 <= 30:
            rsi = "oversold"
        elif row.rsi14 >= 70:
            rsi = "overbought"
        else:
            rsi = "neutral"

        atr_percent = None if row.atr14 is None else row.atr14 / row.close
        if atr_percent is None:
            atr = "unavailable"
        elif atr_percent < Decimal("0.005"):
            atr = "low"
        elif atr_percent < Decimal("0.015"):
            atr = "normal"
        else:
            atr = "high"

        if row.rvol20 is None:
            rvol = "unavailable"
        elif row.rvol20 < Decimal("0.8"):
            rvol = "low"
        elif row.rvol20 < Decimal("1.5"):
            rvol = "normal"
        else:
            rvol = "high"

        if row.sma50 is None:
            price_vs_sma50 = "unavailable"
        elif row.close >= row.sma50:
            price_vs_sma50 = "above"
        else:
            price_vs_sma50 = "below"

        if row.sma20 is None or row.sma50 is None:
            trend = "unavailable"
            strength = "unavailable"
        else:
            if row.close > row.sma20 > row.sma50:
                trend = "bullish"
            elif row.close < row.sma20 < row.sma50:
                trend = "bearish"
            else:
                trend = "mixed"
            spread = abs(row.sma20 - row.sma50) / row.close
            if spread < Decimal("0.005"):
                strength = "weak"
            elif spread < Decimal("0.02"):
                strength = "moderate"
            else:
                strength = "strong"
        return {
            "rsi": rsi,
            "atr_percent": atr,
            "relative_volume": rvol,
            "price_vs_sma50": price_vs_sma50,
            "trend": trend,
            "trend_strength": strength,
        }


def _forward_values(
    rows: Sequence[FeatureRow],
    entry_index: int,
    steps: int,
    side: Signal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    entry_price = rows[entry_index].open
    future = rows[entry_index : entry_index + steps]
    if len(future) != steps:
        raise ValueError("Incomplete future horizon")
    market_return = future[-1].close / entry_price - 1
    maximum_high_return = max(row.high for row in future) / entry_price - 1
    minimum_low_return = min(row.low for row in future) / entry_price - 1
    if side is Signal.BUY:
        return (
            market_return,
            market_return,
            minimum_low_return,
            maximum_high_return,
        )
    if side is Signal.SELL:
        return (
            market_return,
            -market_return,
            -maximum_high_return,
            -minimum_low_return,
        )
    raise ValueError("Event study requires BUY or SELL")


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _share(values) -> Decimal | None:
    items = list(values)
    if not items:
        return None
    return Decimal(sum(items)) / Decimal(len(items))


def _sample_standard_deviation(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = _mean(values)
    if mean is None:
        return None
    variance = sum((value - mean) ** 2 for value in values) / Decimal(
        len(values) - 1
    )
    return variance.sqrt()


def _quantile(values: Sequence[Decimal], probability: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    position = probability * Decimal(len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _overlap_cluster_means(
    edges: Sequence[tuple[int, Decimal]],
    horizon_milliseconds: int,
) -> list[Decimal]:
    return [
        sum(cluster, Decimal("0")) / Decimal(len(cluster))
        for cluster in _overlap_clusters(edges, horizon_milliseconds)
    ]


def _overlap_clusters(
    edges: Sequence[tuple[int, Decimal]],
    horizon_milliseconds: int,
) -> list[list[Decimal]]:
    if not edges:
        return []
    clusters: list[list[Decimal]] = []
    cluster_end = -1
    for entry_time, value in sorted(edges):
        if not clusters or entry_time >= cluster_end:
            clusters.append([value])
            cluster_end = entry_time + horizon_milliseconds
        else:
            clusters[-1].append(value)
            cluster_end = max(cluster_end, entry_time + horizon_milliseconds)
    return clusters


def _bootstrap_interval(
    clusters: Sequence[Sequence[Decimal]],
    samples: int,
    seed: int,
) -> tuple[Decimal | None, Decimal | None]:
    if not clusters:
        return None, None
    generator = random.Random(seed)
    sample_means = []
    count = len(clusters)
    for _ in range(samples):
        selected = [clusters[generator.randrange(count)] for _ in range(count)]
        total = sum(
            (value for cluster in selected for value in cluster),
            Decimal("0"),
        )
        observations = sum(len(cluster) for cluster in selected)
        sample_means.append(total / Decimal(observations))
    return (
        _quantile(sample_means, Decimal("0.025")),
        _quantile(sample_means, Decimal("0.975")),
    )


def _stable_seed(seed: int, *parts: str) -> int:
    payload = "|".join((str(seed), *parts)).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
