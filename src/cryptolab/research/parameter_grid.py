"""Deterministic, bounded parameter grids for strategy research."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from itertools import product

from cryptolab.strategies import (
    BreakoutEventPolicy,
    BreakoutStrategy,
    MeanReversionStrategy,
    TrendExitPolicy,
    TrendFollowingStrategy,
)
from cryptolab.strategies.base import Strategy


ParameterValue = Decimal | int | str


@dataclass(frozen=True)
class ParameterSet:
    """One immutable, serializable strategy configuration."""

    strategy_name: str
    values: tuple[tuple[str, ParameterValue], ...]

    def as_dict(self) -> dict[str, ParameterValue]:
        return dict(self.values)

    @property
    def identifier(self) -> str:
        settings = ",".join(f"{name}={value}" for name, value in self.values)
        return f"{self.strategy_name}[{settings}]"


@dataclass(frozen=True)
class StrategyParameterGrid:
    """Named axes whose Cartesian product is generated in stable order."""

    strategy_name: str
    axes: tuple[tuple[str, tuple[ParameterValue, ...]], ...]

    def combinations(self, max_combinations: int) -> tuple[ParameterSet, ...]:
        if type(max_combinations) is not int or max_combinations <= 0:
            raise ValueError("max_combinations must be a positive integer")
        if not self.axes:
            raise ValueError("A parameter grid must contain at least one axis")
        names = [name for name, _ in self.axes]
        if len(names) != len(set(names)):
            raise ValueError("Parameter-grid axis names must be unique")
        if any(not values for _, values in self.axes):
            raise ValueError("Parameter-grid axes cannot be empty")

        count = 1
        for _, values in self.axes:
            count *= len(values)
        if count > max_combinations:
            raise ValueError(
                f"Grid for {self.strategy_name} contains {count} combinations; "
                f"limit is {max_combinations}"
            )

        result = []
        for values in product(*(values for _, values in self.axes)):
            parameters = ParameterSet(
                strategy_name=self.strategy_name,
                values=tuple(zip(names, values, strict=True)),
            )
            create_strategy(parameters)
            result.append(parameters)
        return tuple(result)


def default_parameter_grids() -> tuple[StrategyParameterGrid, ...]:
    """Return small grids that include every unchanged baseline strategy."""
    return (
        StrategyParameterGrid(
            TrendFollowingStrategy.name,
            (
                ("buy_rsi_min", (Decimal("45"), Decimal("50"))),
                ("buy_rsi_max", (Decimal("65"), Decimal("70"))),
                ("sell_rsi_max", (Decimal("45"), Decimal("50"))),
                (
                    "minimum_relative_volume",
                    (Decimal("0.8"), Decimal("1")),
                ),
                (
                    "exit_policy",
                    (
                        TrendExitPolicy.CONFIRMED.value,
                        TrendExitPolicy.CROSSOVER_ONLY.value,
                    ),
                ),
            ),
        ),
        StrategyParameterGrid(
            MeanReversionStrategy.name,
            (
                ("oversold_rsi", (Decimal("25"), Decimal("30"))),
                ("oversold_recovery_rsi", (Decimal("35"),)),
                ("overbought_recovery_rsi", (Decimal("65"),)),
                ("overbought_rsi", (Decimal("70"), Decimal("75"))),
                (
                    "minimum_sma_deviation",
                    (Decimal("0.02"), Decimal("0.03")),
                ),
                ("recovery_window", (1, 3)),
            ),
        ),
        StrategyParameterGrid(
            BreakoutStrategy.name,
            (
                ("lookback_period", (20, 50)),
                (
                    "minimum_relative_volume",
                    (Decimal("1.2"), Decimal("1.5")),
                ),
                (
                    "minimum_atr_percent",
                    (Decimal("0.003"), Decimal("0.005")),
                ),
                (
                    "event_policy",
                    (
                        BreakoutEventPolicy.NEW_ONLY.value,
                        BreakoutEventPolicy.EVERY_CONFIRMED.value,
                    ),
                ),
            ),
        ),
    )


def create_strategy(parameters: ParameterSet) -> Strategy:
    """Construct a validated strategy from one parameter set."""
    values = parameters.as_dict()
    if parameters.strategy_name == TrendFollowingStrategy.name:
        values = _convert_enum(values, "exit_policy", TrendExitPolicy)
        return TrendFollowingStrategy(**values)
    if parameters.strategy_name == MeanReversionStrategy.name:
        return MeanReversionStrategy(**values)
    if parameters.strategy_name == BreakoutStrategy.name:
        values = _convert_enum(values, "event_policy", BreakoutEventPolicy)
        return BreakoutStrategy(**values)
    raise ValueError(f"Unsupported strategy grid: {parameters.strategy_name}")


def baseline_parameter_set(strategy_name: str) -> ParameterSet:
    """Return the explicit unchanged baseline configuration."""
    if strategy_name == TrendFollowingStrategy.name:
        strategy = TrendFollowingStrategy()
        values: Mapping[str, ParameterValue | Enum] = {
            "buy_rsi_min": strategy.buy_rsi_min,
            "buy_rsi_max": strategy.buy_rsi_max,
            "sell_rsi_max": strategy.sell_rsi_max,
            "minimum_relative_volume": strategy.minimum_relative_volume,
            "exit_policy": strategy.exit_policy,
        }
    elif strategy_name == MeanReversionStrategy.name:
        strategy = MeanReversionStrategy()
        values = {
            "oversold_rsi": strategy.oversold_rsi,
            "oversold_recovery_rsi": strategy.oversold_recovery_rsi,
            "overbought_recovery_rsi": strategy.overbought_recovery_rsi,
            "overbought_rsi": strategy.overbought_rsi,
            "minimum_sma_deviation": strategy.minimum_sma_deviation,
            "recovery_window": strategy.recovery_window,
        }
    elif strategy_name == BreakoutStrategy.name:
        strategy = BreakoutStrategy()
        values = {
            "lookback_period": strategy.lookback_period,
            "minimum_relative_volume": strategy.minimum_relative_volume,
            "minimum_atr_percent": strategy.minimum_atr_percent,
            "event_policy": strategy.event_policy,
        }
    else:
        raise ValueError(f"Unsupported strategy: {strategy_name}")
    return ParameterSet(
        strategy_name,
        tuple(
            (name, value.value if isinstance(value, Enum) else value)
            for name, value in values.items()
        ),
    )


def grid_from_mapping(
    strategy_name: str,
    axes: Mapping[str, Sequence[object]],
) -> StrategyParameterGrid:
    """Parse a JSON-compatible custom grid using known parameter types."""
    baseline = baseline_parameter_set(strategy_name).as_dict()
    unknown = set(axes) - set(baseline)
    if unknown:
        raise ValueError(
            f"Unknown parameters for {strategy_name}: {', '.join(sorted(unknown))}"
        )
    converted = []
    for name, raw_values in axes.items():
        if isinstance(raw_values, (str, bytes)) or not isinstance(
            raw_values, Sequence
        ):
            raise ValueError(f"Grid axis {name} must be a JSON array")
        template = baseline[name]
        converted.append((
            name,
            tuple(_convert_parameter(value, template, name) for value in raw_values),
        ))
    return StrategyParameterGrid(strategy_name, tuple(converted))


def _convert_parameter(
    value: object,
    template: ParameterValue,
    name: str,
) -> ParameterValue:
    if isinstance(template, Decimal):
        try:
            converted = Decimal(str(value))
        except Exception as error:
            raise ValueError(f"Invalid Decimal value for {name}: {value!r}") from error
        if not converted.is_finite():
            raise ValueError(f"Non-finite Decimal value for {name}")
        return converted
    if type(template) is int:
        if type(value) is not int:
            raise ValueError(f"Parameter {name} requires an integer")
        return value
    if not isinstance(value, str):
        raise ValueError(f"Parameter {name} requires a string")
    return value


def _convert_enum(
    values: dict[str, ParameterValue],
    name: str,
    enum_type: type[Enum],
) -> dict[str, object]:
    converted: dict[str, object] = dict(values)
    if name in converted:
        try:
            converted[name] = enum_type(converted[name])
        except ValueError as error:
            raise ValueError(f"Invalid {name}: {converted[name]!r}") from error
    return converted
