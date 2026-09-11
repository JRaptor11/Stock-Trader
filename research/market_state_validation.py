"""Chronological statistical gates for causal daily market-state research."""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict

from research.market_state_episodes import market_state_scorecards


def _mean_test(values: list[float]) -> float | None:
    if len(values) < 2 or statistics.stdev(values) == 0:
        return None
    statistic = statistics.fmean(values) / (statistics.stdev(values) / math.sqrt(len(values)))
    return min(1.0, math.erfc(abs(statistic) / math.sqrt(2.0)))


def _episode_bootstrap(values: list[float], samples: int = 2_000, seed: int = 19) -> dict:
    """Resample whole state episodes so sessions inside an episode stay dependent."""
    if not values:
        return {"ci_low": None, "ci_high": None, "probability_positive": None}
    rng = random.Random(seed)
    estimates = sorted(
        statistics.fmean(rng.choice(values) for _ in values) for _ in range(samples)
    )
    def quantile(p: float) -> float:
        position = (len(estimates) - 1) * p
        low, high = math.floor(position), math.ceil(position)
        if low == high:
            return estimates[low]
        return estimates[low] * (high - position) + estimates[high] * (position - low)
    return {
        "ci_low": quantile(0.025), "ci_high": quantile(0.975),
        "probability_positive": sum(value > 0 for value in estimates) / len(estimates),
    }


def _benjamini_hochberg(rows: list[dict]) -> None:
    eligible = sorted(
        ((index, row["raw_p_value"]) for index, row in enumerate(rows)
         if row["raw_p_value"] is not None), key=lambda item: item[1]
    )
    adjusted = {}
    running = 1.0
    total = len(eligible)
    for rank in range(total, 0, -1):
        index, p_value = eligible[rank - 1]
        running = min(running, p_value * total / rank)
        adjusted[index] = running
    for index, row in enumerate(rows):
        row["bh_adjusted_p_value"] = adjusted.get(index)
        row["passes_fdr_05"] = bool(adjusted.get(index, 1.0) <= 0.05)


def state_validation_outputs(
    daily: list[dict], conditions: dict[str, dict], cost_ladder_bps: tuple[float, ...],
    primary_cost_bps: float, discovery_end: str | None, holdout_start: str | None,
    benchmark_strategy: str = "SPY_BUY_HOLD", fold_sessions: int = 252,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """Build period, episode-inference, transition, and chronological-fold evidence."""
    periods = [("full", None, None)]
    if discovery_end and holdout_start:
        periods += [("discovery", None, discovery_end), ("holdout", holdout_start, None)]

    period_rows, episode_rows = [], []
    for period, start, end in periods:
        _, _, states, episodes = market_state_scorecards(
            daily, conditions, primary_cost_bps, benchmark_strategy,
            period_start=start, period_end=end,
        )
        period_rows.extend({"period": period, **row} for row in states)
        episode_rows.extend({"period": period, **row} for row in episodes)

    benchmark = {(row["period"], row["core_state"], row["episode_id"]): row
                 for row in episode_rows if row["strategy"] == benchmark_strategy}
    inference = []
    groups = defaultdict(list)
    for row in episode_rows:
        if row["strategy"] != benchmark_strategy:
            groups[(row["period"], row["strategy"], row["core_state"])].append(row)
    for (period, strategy, state), rows in sorted(groups.items()):
        excess = []
        for row in rows:
            peer = benchmark.get((period, state, row["episode_id"]))
            if peer:
                excess.append(row["excess_return_vs_spy"])
        boot = _episode_bootstrap(excess)
        inference.append({
            "period": period, "strategy": strategy, "core_state": state,
            "episodes": len(excess), "mean_episode_excess_return": statistics.fmean(excess) if excess else None,
            "median_episode_excess_return": statistics.median(excess) if excess else None,
            "episode_beat_spy_rate": sum(value > 0 for value in excess) / len(excess) if excess else None,
            "raw_p_value": _mean_test(excess), "bootstrap_samples": 2_000,
            "episode_excess_ci_95_low": boot["ci_low"],
            "episode_excess_ci_95_high": boot["ci_high"],
            "bootstrap_probability_excess_positive": boot["probability_positive"],
        })
    # Control false discoveries separately within each genuinely evaluated period.
    for period in {row["period"] for row in inference}:
        _benjamini_hochberg([row for row in inference if row["period"] == period])

    cost_rows = []
    for cost in cost_ladder_bps:
        _, _, states, _ = market_state_scorecards(daily, conditions, cost, benchmark_strategy)
        for row in states:
            cost_rows.append({**row, "is_primary_cost": float(cost) == float(primary_cost_bps)})

    # Stable and transition sessions are evaluated separately using the causal
    # state_changed flag. Daily returns are reconstructed before filtering.
    labels, _, _, _ = market_state_scorecards(daily, conditions, primary_cost_bps, benchmark_strategy)
    labels_by_date = {row["date"]: row for row in labels}
    selected = sorted((row for row in daily if float(row["cost_bps"]) == float(primary_cost_bps)),
                      key=lambda row: (row["strategy"], row["date"]))
    transition_groups, prior = defaultdict(list), {}
    for row in selected:
        strategy, equity = row["strategy"], float(row["equity"])
        previous = prior.get(strategy); prior[strategy] = equity
        label = labels_by_date.get(row["date"])
        if previous and label:
            in_transition = bool(label["state_changed"] or label["pending_core_state"])
            kind = "transition_state_session" if in_transition else "stable_state_session"
            transition_groups[(strategy, kind)].append(equity / previous - 1.0)
    transition_rows = []
    for (strategy, kind), returns in sorted(transition_groups.items()):
        transition_rows.append({
            "strategy": strategy, "session_type": kind, "sessions": len(returns),
            "compounded_return": math.prod(1 + value for value in returns) - 1,
            "mean_daily_return": statistics.fmean(returns),
            "daily_win_rate": sum(value > 0 for value in returns) / len(returns),
        })

    dates = sorted({row["date"] for row in selected if row["date"] in labels_by_date})
    fold_rows = []
    for fold_number, offset in enumerate(range(0, len(dates), fold_sessions), 1):
        fold_dates = dates[offset:offset + fold_sessions]
        if len(fold_dates) < max(30, fold_sessions // 2):
            continue
        fold = f"FOLD:{fold_number:03d}"
        _, _, states, _ = market_state_scorecards(
            daily, conditions, primary_cost_bps, benchmark_strategy,
            minimum_state_sessions=10, minimum_state_episodes=2,
            period_start=fold_dates[0], period_end=fold_dates[-1],
        )
        fold_rows.extend({"fold": fold, "fold_start": fold_dates[0], "fold_end": fold_dates[-1], **row}
                         for row in states)
    return period_rows, inference, cost_rows, transition_rows, fold_rows
