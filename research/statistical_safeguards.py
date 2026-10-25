"""Dependency-light statistical safeguards for broad strategy tournaments.

The helpers in this module are deliberately reporting tools, not promotion
rules. They make the assumptions used by each test explicit and leave the
caller responsible for supplying a complete family of tried strategies.
Omitting discarded variants defeats every multiplicity adjustment here.
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence


_EULER_MASCHERONI = 0.5772156649015329


def _finite_series(values: Sequence[float], name: str) -> list[float]:
    result = [float(value) for value in values]
    if not result:
        raise ValueError(f"{name} must not be empty")
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain only finite returns")
    return result


def _aligned_returns(strategy_returns: Mapping[str, Sequence[float]]) -> dict[str, list[float]]:
    if not strategy_returns:
        raise ValueError("strategy_returns must not be empty")
    aligned = {
        str(name): _finite_series(values, f"strategy_returns[{name!r}]")
        for name, values in strategy_returns.items()
    }
    if len({len(values) for values in aligned.values()}) != 1:
        raise ValueError("all strategy return series must be aligned and have equal length")
    return dict(sorted(aligned.items()))


def _excess_returns(
    strategy_returns: Mapping[str, Sequence[float]], benchmark: Sequence[float] | None,
) -> dict[str, list[float]]:
    aligned = _aligned_returns(strategy_returns)
    if benchmark is None:
        return aligned
    benchmark_values = _finite_series(benchmark, "benchmark")
    observations = len(next(iter(aligned.values())))
    if len(benchmark_values) != observations:
        raise ValueError("benchmark and strategy returns must be aligned")
    return {
        name: [value - benchmark_values[index] for index, value in enumerate(values)]
        for name, values in aligned.items()
    }


def _sharpe(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    deviation = statistics.stdev(values)
    if deviation == 0:
        return None
    return statistics.fmean(values) / deviation


def _selection_score(values: Sequence[float]) -> float:
    """Sharpe-like ordering score with deterministic zero-variance handling."""
    ratio = _sharpe(values)
    if ratio is not None:
        return ratio
    mean = statistics.fmean(values)
    if mean > 0:
        return math.inf
    if mean < 0:
        return -math.inf
    return 0.0


def _reported_score(value: float) -> tuple[float | None, str]:
    """Keep diagnostic JSON standards-compliant when a fold has zero variance."""
    if math.isfinite(value):
        return value, "finite"
    return None, "positive_infinity" if value > 0 else "negative_infinity"


def _population_skewness_and_kurtosis(values: Sequence[float]) -> tuple[float, float]:
    mean = statistics.fmean(values)
    centered = [value - mean for value in values]
    second = statistics.fmean(value * value for value in centered)
    if second == 0:
        return 0.0, 3.0
    third = statistics.fmean(value ** 3 for value in centered)
    fourth = statistics.fmean(value ** 4 for value in centered)
    return third / second ** 1.5, fourth / second ** 2


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def return_evidence(values: list[float], family_trials: int) -> dict:
    n = len(values)
    mean = statistics.fmean(values) if values else None
    if n > 1 and statistics.stdev(values) > 0:
        statistic = mean / (statistics.stdev(values) / math.sqrt(n))
        p_value = min(1.0, math.erfc(abs(statistic) / math.sqrt(2)))
    else:
        p_value = None
    ordered = sorted(values, reverse=True)
    remove_1 = max(1, math.ceil(n * 0.01)) if n else 0
    remove_5 = max(1, math.ceil(n * 0.05)) if n else 0
    compound = lambda rows: math.prod(1 + value for value in rows) - 1 if rows else None
    return {
        "observations": n,
        "mean_return": mean,
        "approximate_two_sided_p": p_value,
        "bonferroni_family_trials": family_trials,
        "bonferroni_adjusted_p": (
            min(1.0, p_value * max(1, family_trials)) if p_value is not None else None
        ),
        "return_without_best_trade": compound(ordered[1:]),
        "return_without_best_1pct": compound(ordered[remove_1:]),
        "return_without_best_5pct": compound(ordered[remove_5:]),
    }


def paired_block_bootstrap(
    candidate: list[float], benchmark: list[float], *,
    block_size: int = 5, samples: int = 2_000, seed: int = 7,
) -> dict:
    """Deterministic moving-block bootstrap for paired session returns."""
    if len(candidate) != len(benchmark):
        raise ValueError("candidate and benchmark returns must be paired")
    if block_size < 1 or samples < 1:
        raise ValueError("block_size and samples must be positive")
    excess = [float(left) - float(right) for left, right in zip(candidate, benchmark)]
    if not excess:
        return {
            "observations": 0, "block_size": block_size, "samples": samples,
            "mean_excess_return": None, "excess_return_ci_95": None,
            "probability_mean_excess_positive": None,
        }
    rng = random.Random(seed)
    n = len(excess)
    starts = range(max(1, n - block_size + 1))
    estimates = []
    for _ in range(samples):
        draw = []
        while len(draw) < n:
            start = rng.choice(starts)
            draw.extend(excess[start:min(n, start + block_size)])
        estimates.append(statistics.fmean(draw[:n]))
    return {
        "observations": n, "block_size": block_size, "samples": samples,
        "mean_excess_return": statistics.fmean(excess),
        "excess_return_ci_95": [_quantile(estimates, 0.025), _quantile(estimates, 0.975)],
        "probability_mean_excess_positive": (
            sum(value > 0 for value in estimates) / len(estimates)
        ),
    }


def deflated_sharpe_ratio(
    strategy_returns: Mapping[str, Sequence[float]], *,
    selected_strategy: str | None = None,
    benchmark: Sequence[float] | None = None,
    family_trials: float | None = None,
    benchmark_sharpe: float = 0.0,
    periods_per_year: int = 252,
) -> dict:
    """Report Bailey/Lopez de Prado's Deflated Sharpe Ratio probability.

    Returns are converted to arithmetic excess returns when ``benchmark`` is
    supplied. ``strategy_returns`` must contain the complete family of tried
    variants, including discarded variants. ``family_trials`` is the number of
    *independent effective* trials used in the expected-maximum calculation;
    it defaults to the observed variant count, and may be replaced only by a
    separately justified effective-trial estimate.
    """
    if periods_per_year < 1:
        raise ValueError("periods_per_year must be positive")
    excess = _excess_returns(strategy_returns, benchmark)
    observations = len(next(iter(excess.values())))
    if observations < 3:
        raise ValueError("deflated Sharpe requires at least three aligned observations")
    sharpes = {name: _sharpe(values) for name, values in excess.items()}
    valid = {name: value for name, value in sharpes.items() if value is not None}
    if not valid:
        raise ValueError("at least one strategy must have non-zero return variance")
    if selected_strategy is None:
        selected_strategy = max(sorted(valid), key=lambda name: valid[name])
    if selected_strategy not in excess:
        raise ValueError("selected_strategy is not present in strategy_returns")
    selected_sharpe = sharpes[selected_strategy]
    if selected_sharpe is None:
        raise ValueError("selected strategy has zero return variance")

    trials = float(len(excess) if family_trials is None else family_trials)
    if not math.isfinite(trials) or trials < 1:
        raise ValueError("family_trials must be finite and at least one")
    trial_sharpes = list(valid.values())
    dispersion = statistics.stdev(trial_sharpes) if len(trial_sharpes) > 1 else 0.0
    threshold = float(benchmark_sharpe)
    if trials > 1 and dispersion > 0:
        normal = statistics.NormalDist()
        first = normal.inv_cdf(1.0 - 1.0 / trials)
        second = normal.inv_cdf(1.0 - 1.0 / (trials * math.e))
        threshold += dispersion * (
            (1.0 - _EULER_MASCHERONI) * first + _EULER_MASCHERONI * second
        )

    skewness, kurtosis = _population_skewness_and_kurtosis(excess[selected_strategy])
    variance_adjustment = (
        1.0 - skewness * selected_sharpe
        + ((kurtosis - 1.0) / 4.0) * selected_sharpe ** 2
    )
    if variance_adjustment <= 0 or not math.isfinite(variance_adjustment):
        probability = None
        reason = "non_positive_probabilistic_sharpe_variance"
    else:
        statistic = (
            (selected_sharpe - threshold) * math.sqrt(observations - 1)
            / math.sqrt(variance_adjustment)
        )
        probability = statistics.NormalDist().cdf(statistic)
        reason = None
    annualizer = math.sqrt(periods_per_year)
    return {
        "method": "Bailey-Lopez-de-Prado deflated Sharpe ratio",
        "diagnostic_only": True,
        "selected_strategy": selected_strategy,
        "observations": observations,
        "observed_strategies": len(excess),
        "valid_trial_sharpes": len(valid),
        "family_trials": trials,
        "returns_are_excess_to_benchmark": benchmark is not None,
        "selected_periodic_sharpe": selected_sharpe,
        "selected_annualized_sharpe": selected_sharpe * annualizer,
        "expected_max_periodic_sharpe_under_null": threshold,
        "expected_max_annualized_sharpe_under_null": threshold * annualizer,
        "trial_periodic_sharpe_dispersion": dispersion,
        "trial_sharpe_dispersion_estimated": len(trial_sharpes) > 1,
        "zero_variance_trials_excluded_from_dispersion": sorted(
            name for name, value in sharpes.items() if value is None
        ),
        "selected_return_skewness": skewness,
        "selected_return_kurtosis": kurtosis,
        "deflated_sharpe_probability": probability,
        "passes_95_percent_probability": bool(probability is not None and probability >= 0.95),
        "insufficiency_reason": reason,
        "assumptions": [
            "strategy_returns includes every tried variant, including discarded variants",
            "the expected-maximum threshold treats family_trials as independent effective trials",
            "cross-trial Sharpe dispersion is estimated from supplied non-degenerate variants",
            "the selected return observations are identically distributed for the PSR approximation",
        ],
    }


def _midrank(value: float, population: Sequence[float]) -> float:
    below = sum(item < value for item in population)
    equal = sum(item == value for item in population)
    return below + (equal + 1.0) / 2.0


def combinatorially_symmetric_cross_validation(
    strategy_returns: Mapping[str, Sequence[float]], *,
    benchmark: Sequence[float] | None = None,
    partitions: int = 10,
    max_splits: int = 20_000,
) -> dict:
    """Estimate Probability of Backtest Overfitting using CSCV.

    The aligned history is split into contiguous, approximately equal blocks.
    Every combination of half the blocks is used as in-sample data and its
    complement as out-of-sample data. Strategies are selected by periodic
    Sharpe, then their out-of-sample rank is transformed to the PBO logit.
    """
    excess = _excess_returns(strategy_returns, benchmark)
    names = sorted(excess)
    if len(names) < 2:
        raise ValueError("CSCV requires at least two strategy variants")
    observations = len(next(iter(excess.values())))
    if partitions < 4 or partitions % 2:
        raise ValueError("partitions must be an even integer of at least four")
    if observations < partitions:
        raise ValueError("observations must be at least the number of partitions")
    split_count = math.comb(partitions, partitions // 2)
    if split_count > max_splits:
        raise ValueError(
            f"CSCV would create {split_count} splits, above max_splits={max_splits}"
        )
    blocks = [
        list(range(index * observations // partitions, (index + 1) * observations // partitions))
        for index in range(partitions)
    ]
    split_rows = []
    selections = Counter()
    for train_blocks in itertools.combinations(range(partitions), partitions // 2):
        train_set = set(train_blocks)
        train_index = [item for block in train_blocks for item in blocks[block]]
        test_index = [
            item for block in range(partitions) if block not in train_set for item in blocks[block]
        ]
        train_scores = {
            name: _selection_score([excess[name][index] for index in train_index])
            for name in names
        }
        selected = max(names, key=lambda name: train_scores[name])
        selections[selected] += 1
        test_scores = {
            name: _selection_score([excess[name][index] for index in test_index])
            for name in names
        }
        rank = _midrank(test_scores[selected], list(test_scores.values()))
        relative_rank = rank / (len(names) + 1.0)
        logit = math.log(relative_rank / (1.0 - relative_rank))
        train_score = train_scores[selected]
        test_score = test_scores[selected]
        reported_train_score, train_score_status = _reported_score(train_score)
        reported_test_score, test_score_status = _reported_score(test_score)
        split_rows.append({
            "train_blocks": list(train_blocks),
            "selected_strategy": selected,
            "in_sample_score": reported_train_score,
            "in_sample_score_status": train_score_status,
            "out_of_sample_score": reported_test_score,
            "out_of_sample_score_status": test_score_status,
            "out_of_sample_rank_worst_to_best": rank,
            "out_of_sample_relative_rank": relative_rank,
            "logit": logit,
            "classified_overfit": logit <= 0.0,
            "selected_strategy_loses_out_of_sample": test_score < 0.0,
        })
    logits = [row["logit"] for row in split_rows]
    return {
        "method": "combinatorially symmetric cross-validation probability of backtest overfitting",
        "diagnostic_only": True,
        "observations": observations,
        "strategies": len(names),
        "partitions": partitions,
        "partition_sizes": [len(block) for block in blocks],
        "split_count": len(split_rows),
        "selection_metric": "periodic arithmetic Sharpe ratio",
        "returns_are_excess_to_benchmark": benchmark is not None,
        "probability_of_backtest_overfitting": (
            sum(row["classified_overfit"] for row in split_rows) / len(split_rows)
        ),
        "median_logit": statistics.median(logits),
        "mean_logit": statistics.fmean(logits),
        "probability_selected_strategy_loses_out_of_sample": (
            sum(row["selected_strategy_loses_out_of_sample"] for row in split_rows)
            / len(split_rows)
        ),
        "selection_counts": dict(sorted(selections.items())),
        "split_results": split_rows,
        "assumptions": [
            "all tried variants use the same aligned observations",
            "contiguous partitions preserve local dependence but CSCV does not preserve a single chronology",
            "midranks use rank divided by strategies plus one to keep logits finite",
            "a positive or negative constant fold is ranked as positive or negative infinite Sharpe",
            "PBO diagnoses selection overfit and is not a conventional null-hypothesis p-value",
        ],
    }


def _stationary_bootstrap_indices(
    observations: int, mean_block_length: float, rng: random.Random,
) -> list[int]:
    restart_probability = 1.0 / mean_block_length
    indices = []
    current = 0
    for position in range(observations):
        if position == 0 or rng.random() < restart_probability:
            current = rng.randrange(observations)
        else:
            current = (current + 1) % observations
        indices.append(current)
    return indices


def white_reality_check(
    strategy_returns: Mapping[str, Sequence[float]], benchmark: Sequence[float], *,
    bootstrap_samples: int = 2_000,
    mean_block_length: float = 10.0,
    seed: int = 29,
    periods_per_year: int = 252,
) -> dict:
    """White Reality Check using a joint stationary bootstrap.

    The statistic is the maximum arithmetic mean return differential over the
    family. Centered return vectors use identical stationary-bootstrap indices,
    retaining serial and contemporaneous cross-strategy dependence. This is
    White's unstudentized Reality Check, not Hansen's SPA refinement.
    """
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    if not math.isfinite(mean_block_length) or mean_block_length < 1:
        raise ValueError("mean_block_length must be finite and at least one")
    if periods_per_year < 1:
        raise ValueError("periods_per_year must be positive")
    excess = _excess_returns(strategy_returns, benchmark)
    observations = len(next(iter(excess.values())))
    if observations < 2:
        raise ValueError("Reality Check requires at least two aligned observations")
    names = sorted(excess)
    means = {name: statistics.fmean(excess[name]) for name in names}
    best = max(names, key=lambda name: means[name])
    observed_statistic = max(0.0, math.sqrt(observations) * means[best])
    centered = {
        name: [value - means[name] for value in values]
        for name, values in excess.items()
    }
    rng = random.Random(seed)
    bootstrap_statistics = []
    for _ in range(bootstrap_samples):
        indices = _stationary_bootstrap_indices(observations, mean_block_length, rng)
        statistic = max(
            math.sqrt(observations)
            * statistics.fmean(centered[name][index] for index in indices)
            for name in names
        )
        bootstrap_statistics.append(max(0.0, statistic))
    exceedances = sum(value >= observed_statistic for value in bootstrap_statistics)
    p_value = (exceedances + 1.0) / (bootstrap_samples + 1.0)
    return {
        "method": "White Reality Check with joint stationary bootstrap",
        "diagnostic_only": True,
        "observations": observations,
        "strategies": len(names),
        "best_strategy": best,
        "best_mean_periodic_excess_return": means[best],
        "best_annualized_arithmetic_excess_return": means[best] * periods_per_year,
        "observed_test_statistic": observed_statistic,
        "bootstrap_samples": bootstrap_samples,
        "mean_block_length": mean_block_length,
        "seed": seed,
        "p_value": p_value,
        "rejects_equal_or_inferior_family_at_05": p_value <= 0.05,
        "bootstrap_critical_value_95": _quantile(bootstrap_statistics, 0.95),
        "bootstrap_critical_value_99": _quantile(bootstrap_statistics, 0.99),
        "strategy_mean_excess_returns": means,
        "assumptions": [
            "the supplied family includes every tried variant",
            "all strategy and benchmark returns are aligned without survivorship filtering",
            "the stationary bootstrap's mean block length is adequate for serial dependence",
            "the unstudentized test can favor high-volatility variants; Hansen SPA is not implemented here",
        ],
    }


def multiple_testing_report(
    strategy_returns: Mapping[str, Sequence[float]], benchmark: Sequence[float], *,
    selected_strategy: str | None = None,
    family_trials: float | None = None,
    periods_per_year: int = 252,
    cscv_partitions: int = 10,
    bootstrap_samples: int = 2_000,
    mean_block_length: float = 10.0,
    seed: int = 29,
) -> dict:
    """Create one serializable DSR, CSCV-PBO, and Reality Check report."""
    return {
        "schema_version": 1,
        "diagnostic_only": True,
        "deflated_sharpe": deflated_sharpe_ratio(
            strategy_returns,
            selected_strategy=selected_strategy,
            benchmark=benchmark,
            family_trials=family_trials,
            periods_per_year=periods_per_year,
        ),
        "cscv_pbo": combinatorially_symmetric_cross_validation(
            strategy_returns, benchmark=benchmark, partitions=cscv_partitions,
        ),
        "white_reality_check": white_reality_check(
            strategy_returns, benchmark,
            bootstrap_samples=bootstrap_samples,
            mean_block_length=mean_block_length,
            seed=seed,
            periods_per_year=periods_per_year,
        ),
    }
