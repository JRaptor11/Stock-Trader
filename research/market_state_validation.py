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


def _fold_recurrence(fold_rows: list[dict], benchmark_strategy: str) -> list[dict]:
    groups = defaultdict(list)
    for row in fold_rows:
        if row["strategy"] != benchmark_strategy and row["sample_sufficient"]:
            groups[(row["strategy"], row["core_state"])].append(row)
    result = []
    for (strategy, state), rows in sorted(groups.items()):
        excess = [float(row["relative_wealth_vs_spy"]) for row in rows]
        result.append({
            "strategy": strategy, "core_state": state,
            "eligible_folds": len(rows),
            "positive_excess_folds": sum(value > 0 for value in excess),
            "fold_win_rate": sum(value > 0 for value in excess) / len(excess),
            "mean_fold_relative_wealth_vs_spy": statistics.fmean(excess),
            "median_fold_relative_wealth_vs_spy": statistics.median(excess),
            "worst_fold_relative_wealth_vs_spy": min(excess),
            "best_fold_relative_wealth_vs_spy": max(excess),
            "recurs_in_chronological_folds": (
                len(rows) >= 3
                and sum(value > 0 for value in excess) / len(excess) >= 0.60
                and statistics.median(excess) > 0
            ),
        })
    return result


def _transition_horizons(
    daily_returns: dict[tuple[str, str], float], labels_by_date: dict[str, dict],
    strategies: list[str], benchmark_strategy: str, horizons: tuple[int, ...] = (1, 3, 5, 10),
) -> list[dict]:
    dates = sorted(labels_by_date)
    events = [(index, day, labels_by_date[day]["core_state"])
              for index, day in enumerate(dates) if labels_by_date[day]["state_changed"]]
    groups = defaultdict(list)
    for index, event_date, state in events:
        for horizon in horizons:
            window = dates[index:index + horizon]
            if len(window) != horizon:
                continue
            benchmark_values = [daily_returns.get((benchmark_strategy, day)) for day in window]
            if any(value is None for value in benchmark_values):
                continue
            benchmark_return = math.prod(1 + value for value in benchmark_values) - 1
            for strategy in strategies:
                if strategy == benchmark_strategy:
                    continue
                values = [daily_returns.get((strategy, day)) for day in window]
                if any(value is None for value in values):
                    continue
                strategy_return = math.prod(1 + value for value in values) - 1
                excess = (1 + strategy_return) / (1 + benchmark_return) - 1
                groups[(strategy, state, horizon)].append((event_date, excess))
    result = []
    for (strategy, state, horizon), observations in sorted(groups.items()):
        excess = [value for _, value in observations]
        boot = _episode_bootstrap(excess)
        result.append({
            "strategy": strategy, "entered_core_state": state,
            "horizon_sessions": horizon, "transition_events": len(excess),
            "first_event_date": observations[0][0], "last_event_date": observations[-1][0],
            "mean_relative_wealth_vs_spy": statistics.fmean(excess),
            "median_relative_wealth_vs_spy": statistics.median(excess),
            "event_beat_spy_rate": sum(value > 0 for value in excess) / len(excess),
            "raw_p_value": _mean_test(excess),
            "event_excess_ci_95_low": boot["ci_low"],
            "event_excess_ci_95_high": boot["ci_high"],
            "bootstrap_probability_excess_positive": boot["probability_positive"],
        })
    for horizon in horizons:
        _benjamini_hochberg([row for row in result if row["horizon_sessions"] == horizon])
    return result


def _survival_table(
    period_rows: list[dict], inference: list[dict], cost_rows: list[dict],
    recurrence: list[dict], benchmark_strategy: str,
) -> list[dict]:
    holdout = {(row["strategy"], row["core_state"]): row for row in period_rows
               if row["period"] == "holdout" and row["strategy"] != benchmark_strategy}
    tests = {(row["strategy"], row["core_state"]): row for row in inference
             if row["period"] == "holdout"}
    folds = {(row["strategy"], row["core_state"]): row for row in recurrence}
    high_cost = {(row["strategy"], row["core_state"]): row for row in cost_rows
                 if row["period"] == "holdout" and float(row["cost_bps"]) == 20.0}
    result = []
    for key, row in sorted(holdout.items()):
        test, fold, cost = tests.get(key, {}), folds.get(key, {}), high_cost.get(key, {})
        adequate = bool(row["sample_sufficient"])
        positive = float(row["relative_wealth_vs_spy"]) > 0
        corrected = bool(test.get("passes_fdr_05"))
        ci_positive = (test.get("episode_excess_ci_95_low") is not None
                       and float(test["episode_excess_ci_95_low"]) > 0)
        promising = adequate and positive and corrected and ci_positive
        recurring = promising and bool(fold.get("recurs_in_chronological_folds"))
        robust = recurring and bool(cost) and float(cost["relative_wealth_vs_spy"]) > 0
        if not adequate:
            status = "INSUFFICIENT_EVIDENCE"
        elif not promising:
            status = "FAILED_STATISTICAL_GATES"
        elif not recurring:
            status = "HISTORICALLY_PROMISING"
        elif not robust:
            status = "CHRONOLOGICALLY_RECURRING"
        else:
            status = "COST_ROBUST_AWAITING_FORWARD_VALIDATION"
        result.append({
            "strategy": key[0], "core_state": key[1], "status": status,
            "holdout_sessions": row["sessions"], "holdout_episodes": row["episodes"],
            "holdout_relative_wealth_vs_spy": row["relative_wealth_vs_spy"],
            "holdout_fdr_pass": corrected, "holdout_episode_ci_low_above_zero": ci_positive,
            "eligible_chronological_folds": fold.get("eligible_folds", 0),
            "chronological_fold_win_rate": fold.get("fold_win_rate"),
            "positive_at_20_bps": bool(cost) and float(cost["relative_wealth_vs_spy"]) > 0,
            "forward_observations": 0, "routing_eligible": False,
        })
    return result


def state_validation_outputs(
    daily: list[dict], conditions: dict[str, dict], cost_ladder_bps: tuple[float, ...],
    primary_cost_bps: float, discovery_end: str | None, holdout_start: str | None,
    benchmark_strategy: str = "SPY_BUY_HOLD", fold_sessions: int = 252,
    progress_callback=None,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """Build period, episode-inference, transition, and chronological-fold evidence."""
    periods = [("full", None, None)]
    if discovery_end and holdout_start:
        periods += [("discovery", None, discovery_end), ("holdout", holdout_start, None)]

    period_rows, episode_rows = [], []
    for period_number, (period, start, end) in enumerate(periods, 1):
        _, _, states, episodes = market_state_scorecards(
            daily, conditions, primary_cost_bps, benchmark_strategy,
            period_start=start, period_end=end,
        )
        period_rows.extend({"period": period, **row} for row in states)
        episode_rows.extend({"period": period, **row} for row in episodes)
        if progress_callback:
            progress_callback({"stage": "market_state_periods", "stage_completed_rows": period_number,
                               "stage_total_rows": len(periods),
                               "stage_percent_complete": round(period_number / len(periods) * 100, 2)})

    benchmark = {(row["period"], row["core_state"], row["episode_id"]): row
                 for row in episode_rows if row["strategy"] == benchmark_strategy}
    inference = []
    groups = defaultdict(list)
    for row in episode_rows:
        if row["strategy"] != benchmark_strategy:
            groups[(row["period"], row["strategy"], row["core_state"])].append(row)
    ordered_groups = sorted(groups.items())
    for group_number, ((period, strategy, state), rows) in enumerate(ordered_groups, 1):
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
        if progress_callback and (group_number == len(ordered_groups) or group_number % 20 == 0):
            progress_callback({"stage": "market_state_inference", "stage_completed_rows": group_number,
                               "stage_total_rows": len(ordered_groups),
                               "stage_percent_complete": round(group_number / len(ordered_groups) * 100, 2)})
    # Control false discoveries separately within each genuinely evaluated period.
    for period in {row["period"] for row in inference}:
        _benjamini_hochberg([row for row in inference if row["period"] == period])

    cost_rows = []
    for cost_number, cost in enumerate(cost_ladder_bps, 1):
        for period, start, end in periods:
            _, _, states, _ = market_state_scorecards(
                daily, conditions, cost, benchmark_strategy,
                period_start=start, period_end=end,
            )
            for row in states:
                cost_rows.append({"period": period, **row,
                                  "is_primary_cost": float(cost) == float(primary_cost_bps)})
        if progress_callback:
            progress_callback({"stage": "market_state_cost_sensitivity",
                               "stage_completed_rows": cost_number,
                               "stage_total_rows": len(cost_ladder_bps),
                               "stage_percent_complete": round(cost_number / len(cost_ladder_bps) * 100, 2)})

    # Stable and transition sessions are evaluated separately using the causal
    # state_changed flag. Daily returns are reconstructed before filtering.
    labels, _, _, _ = market_state_scorecards(daily, conditions, primary_cost_bps, benchmark_strategy)
    labels_by_date = {row["date"]: row for row in labels}
    selected = sorted((row for row in daily if float(row["cost_bps"]) == float(primary_cost_bps)),
                      key=lambda row: (row["strategy"], row["date"]))
    transition_groups, prior, daily_returns = defaultdict(list), {}, {}
    for row in selected:
        strategy, equity = row["strategy"], float(row["equity"])
        previous = prior.get(strategy); prior[strategy] = equity
        label = labels_by_date.get(row["date"])
        if previous and label:
            daily_returns[(strategy, row["date"])] = equity / previous - 1.0
            in_transition = bool(label["state_changed"] or label["pending_core_state"])
            kind = "transition_state_session" if in_transition else "stable_state_session"
            transition_groups[(strategy, kind)].append(daily_returns[(strategy, row["date"])])
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
    recurrence = _fold_recurrence(fold_rows, benchmark_strategy)
    if progress_callback:
        progress_callback({"stage": "market_state_chronological_folds",
                           "stage_completed_rows": len(fold_rows),
                           "stage_total_rows": len(fold_rows), "stage_percent_complete": 100.0})
    strategies = sorted({row["strategy"] for row in selected})
    transition_horizons = _transition_horizons(
        daily_returns, labels_by_date, strategies, benchmark_strategy,
    )
    survival = _survival_table(
        period_rows, inference, cost_rows, recurrence, benchmark_strategy,
    ) if holdout_start else []
    if progress_callback:
        progress_callback({"stage": "market_state_validation_complete", "stage_percent_complete": 100.0})
    return (period_rows, inference, cost_rows, transition_rows, fold_rows,
            recurrence, transition_horizons, survival)
