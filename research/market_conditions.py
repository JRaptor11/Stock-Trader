"""Causal market-state features and strategy-condition diagnostics.

Every feature assigned to session *t* is calculated only from bars through
session *t-1*.  The raw continuous values are retained alongside expanding
percentile buckets so later research is not constrained to four coarse
bull/bear volatility labels.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict


BUCKET_LABELS = ("Q1_LOW", "Q2", "Q3", "Q4", "Q5_HIGH")
DEFAULT_PAIR_DIMENSIONS = (
    ("trend_200d_distance_bucket", "volatility_20d_bucket"),
    ("breadth_50d_bucket", "dispersion_20d_bucket"),
    ("volatility_20d_bucket", "volatility_expansion_bucket"),
    ("trend_20d_return_bucket", "breadth_50d_bucket"),
)


def _returns(values: list[float], window: int) -> list[float]:
    start = max(1, len(values) - window)
    return [values[i] / values[i - 1] - 1.0 for i in range(start, len(values)) if values[i - 1]]


def _distance_from_mean(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    average = statistics.fmean(values[-window:])
    return values[-1] / average - 1.0 if average else None


def _period_return(values: list[float], window: int) -> float | None:
    return values[-1] / values[-window - 1] - 1.0 if len(values) > window and values[-window - 1] else None


def _annualized_volatility(values: list[float], window: int) -> float | None:
    returns = _returns(values, window)
    return statistics.stdev(returns) * math.sqrt(252) if len(returns) >= window - 1 and len(returns) > 1 else None


def _percentile_bucket(value: float | None, history: list[float], minimum_history: int) -> tuple[float | None, str | None]:
    if value is None or len(history) < minimum_history:
        return None, None
    below = sum(item < value for item in history)
    equal = sum(item == value for item in history)
    percentile = (below + 0.5 * equal) / len(history)
    index = min(4, int(percentile * 5))
    return percentile, BUCKET_LABELS[index]


def _cross_sectional_features(histories: dict[str, list[float]], benchmark: str) -> dict[str, float | None]:
    eligible = [values for symbol, values in histories.items() if symbol != benchmark and len(values) >= 51]
    breadth_20 = [values[-1] > statistics.fmean(values[-20:]) for values in eligible]
    breadth_50 = [values[-1] > statistics.fmean(values[-50:]) for values in eligible]
    one_day = [values[-1] / values[-2] - 1.0 for values in eligible if values[-2]]
    twenty_day = [values[-1] / values[-21] - 1.0 for values in eligible if values[-21]]
    return {
        "breadth_20d": sum(breadth_20) / len(breadth_20) if breadth_20 else None,
        "breadth_50d": sum(breadth_50) / len(breadth_50) if breadth_50 else None,
        "dispersion_1d": statistics.stdev(one_day) if len(one_day) > 1 else None,
        "dispersion_20d": statistics.stdev(twenty_day) if len(twenty_day) > 1 else None,
    }


def causal_market_conditions(
    dates: list[str], bars: dict, symbols: tuple[str, ...], benchmark: str = "SPY",
    minimum_bucket_history: int = 60,
) -> dict[str, dict]:
    """Return a causal continuous feature snapshot and expanding buckets per day."""
    histories = {symbol: [] for symbol in symbols}
    feature_history: dict[str, list[float]] = defaultdict(list)
    conditions: dict[str, dict] = {}
    previous_gap = None
    for day in dates:
        benchmark_history = histories.get(benchmark, [])
        if benchmark_history:
            vol20 = _annualized_volatility(benchmark_history, 20)
            vol60 = _annualized_volatility(benchmark_history, 60)
            snapshot = {
                "date": day,
                "trend_20d_return": _period_return(benchmark_history, 20),
                "trend_63d_return": _period_return(benchmark_history, 63),
                "trend_200d_distance": _distance_from_mean(benchmark_history, 200),
                "volatility_20d": vol20,
                "volatility_60d": vol60,
                "volatility_expansion": vol20 / vol60 if vol20 is not None and vol60 else None,
                "prior_market_gap": previous_gap,
                **_cross_sectional_features(histories, benchmark),
            }
            for feature, value in tuple(snapshot.items()):
                if feature == "date":
                    continue
                percentile, bucket = _percentile_bucket(value, feature_history[feature], minimum_bucket_history)
                snapshot[f"{feature}_percentile"] = percentile
                snapshot[f"{feature}_bucket"] = bucket
            conditions[day] = snapshot
            for feature, value in snapshot.items():
                if feature != "date" and not feature.endswith(("_bucket", "_percentile")) and value is not None:
                    feature_history[feature].append(float(value))

        today = bars[day]
        if benchmark in today and benchmark_history:
            previous_gap = today[benchmark]["open"] / benchmark_history[-1] - 1.0
        for symbol in symbols:
            if symbol in today:
                histories[symbol].append(float(today[symbol]["close"]))
    return conditions


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position)); upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _distribution(returns: list[float], minimum_samples: int) -> dict:
    positives = sorted((value for value in returns if value > 0), reverse=True)
    gross_profit = sum(positives)
    top_1 = max(1, math.ceil(len(returns) * 0.01)) if returns else 0
    top_5 = max(1, math.ceil(len(returns) * 0.05)) if returns else 0
    return {
        "sessions": len(returns),
        "minimum_samples": minimum_samples,
        "sample_sufficient": len(returns) >= minimum_samples,
        "compounded_return": math.prod(1.0 + value for value in returns) - 1.0,
        "mean_return": statistics.fmean(returns),
        "median_return": statistics.median(returns),
        "win_rate": sum(value > 0 for value in returns) / len(returns),
        "p05_return": _quantile(returns, 0.05),
        "p25_return": _quantile(returns, 0.25),
        "p75_return": _quantile(returns, 0.75),
        "p95_return": _quantile(returns, 0.95),
        "worst_return": min(returns),
        "best_return": max(returns),
        "top_1pct_gross_profit_share": sum(positives[:top_1]) / gross_profit if gross_profit else None,
        "top_5pct_gross_profit_share": sum(positives[:top_5]) / gross_profit if gross_profit else None,
    }


def condition_scorecards(
    daily: list[dict], conditions: dict[str, dict], primary_cost_bps: float,
    minimum_samples: int = 30, pair_dimensions=DEFAULT_PAIR_DIMENSIONS,
) -> tuple[list[dict], list[dict]]:
    """Build single-dimension and predeclared two-dimension distribution tables."""
    selected = sorted(
        (row for row in daily if float(row["cost_bps"]) == float(primary_cost_bps)),
        key=lambda row: (row["strategy"], row["date"]),
    )
    observations = []
    prior_equity: dict[str, float] = {}
    for row in selected:
        strategy = row["strategy"]
        prior = prior_equity.get(strategy)
        prior_equity[strategy] = float(row["equity"])
        state = conditions.get(row["date"])
        if prior is None or not prior or not state:
            continue
        observations.append((strategy, row["date"], float(row["equity"]) / prior - 1.0, state))

    bucket_dimensions = sorted({
        key for state in conditions.values() for key in state if key.endswith("_bucket")
    })
    singles: dict[tuple, list[float]] = defaultdict(list)
    pairs: dict[tuple, list[float]] = defaultdict(list)
    for strategy, _day, value, state in observations:
        for dimension in bucket_dimensions:
            bucket = state.get(dimension)
            if bucket:
                singles[(strategy, dimension, bucket)].append(value)
        for first, second in pair_dimensions:
            first_bucket, second_bucket = state.get(first), state.get(second)
            if first_bucket and second_bucket:
                pairs[(strategy, first, first_bucket, second, second_bucket)].append(value)

    single_rows = [
        {"strategy": key[0], "dimension": key[1], "bucket": key[2],
         "cost_bps": primary_cost_bps, **_distribution(values, minimum_samples)}
        for key, values in sorted(singles.items())
    ]
    pair_rows = [
        {"strategy": key[0], "dimension_1": key[1], "bucket_1": key[2],
         "dimension_2": key[3], "bucket_2": key[4], "cost_bps": primary_cost_bps,
         **_distribution(values, minimum_samples)}
        for key, values in sorted(pairs.items())
    ]
    return single_rows, pair_rows
