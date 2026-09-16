"""Deep, role-specific diagnostics without constructing a strategy router."""

from __future__ import annotations

import statistics
from collections import defaultdict

from research.role_aware_evidence import strategy_role


HORIZONS = (1, 2, 3, 5, 10)


def _f(value):
    return None if value in (None, "") else float(value)


def build_baseline_state_leaderboard(config, state_periods, state_survival, baseline_strategy):
    primary = float(config.primary_cost_bps)
    roles = {name: strategy_role(name) for name in config.strategy_names}
    survival = {(row["strategy"], row["core_state"]): row for row in state_survival}
    grouped = defaultdict(list)
    for row in state_periods:
        if (float(row["cost_bps"]) == primary
                and roles.get(row["strategy"]) == "baseline_candidate"):
            grouped[(row["period"], row["core_state"])].append(row)
    output = []
    for (period, state), rows in sorted(grouped.items()):
        comparator = next((row for row in rows if row["strategy"] == baseline_strategy), None)
        ranked = sorted(rows, key=lambda row: _f(row["relative_wealth_vs_spy"]) or -10, reverse=True)
        for rank, row in enumerate(ranked, 1):
            strategy_return = _f(row["strategy_compounded_return"])
            comparator_return = _f(comparator["strategy_compounded_return"]) if comparator else None
            relative = ((1 + strategy_return) / (1 + comparator_return) - 1
                        if comparator_return is not None and strategy_return is not None else None)
            evidence = survival.get((row["strategy"], state), {})
            output.append({
                "period": period, "core_state": state, "strategy": row["strategy"],
                "rank_within_state": rank, "sessions": row["sessions"], "episodes": row["episodes"],
                "strategy_compounded_return": strategy_return,
                "spy_compounded_return": _f(row["spy_compounded_return"]),
                "relative_wealth_vs_spy": _f(row["relative_wealth_vs_spy"]),
                "baseline_comparator": baseline_strategy,
                "relative_wealth_vs_default_baseline": relative,
                "positive_episode_rate": _f(row["positive_episode_rate"]),
                "episode_beat_spy_rate": _f(row["episode_beat_spy_rate"]),
                "survival_status": evidence.get("status"),
                "chronological_fold_win_rate": _f(evidence.get("chronological_fold_win_rate")),
                "positive_at_20_bps": evidence.get("positive_at_20_bps"),
            })
    return output


def build_defensive_baseline_comparisons(config, state_periods, baseline_strategy):
    roles = {name: strategy_role(name) for name in config.strategy_names}
    indexed = {(row["period"], row["core_state"], row["strategy"], float(row["cost_bps"])): row
               for row in state_periods}
    output = []
    for row in state_periods:
        strategy = row["strategy"]
        cost = float(row["cost_bps"])
        if cost != float(config.primary_cost_bps) or roles.get(strategy) != "defensive_override":
            continue
        baseline = indexed.get((row["period"], row["core_state"], baseline_strategy, cost))
        baseline_20 = indexed.get((row["period"], row["core_state"], baseline_strategy, 20.0))
        strategy_20 = indexed.get((row["period"], row["core_state"], strategy, 20.0))
        if not baseline:
            continue
        strategy_return = _f(row["strategy_compounded_return"])
        baseline_return = _f(baseline["strategy_compounded_return"])
        relative = (1 + strategy_return) / (1 + baseline_return) - 1
        relative_20 = None
        if baseline_20 and strategy_20:
            relative_20 = ((1 + _f(strategy_20["strategy_compounded_return"]))
                           / (1 + _f(baseline_20["strategy_compounded_return"])) - 1)
        output.append({
            "period": row["period"], "core_state": row["core_state"],
            "strategy": strategy, "baseline_strategy": baseline_strategy,
            "sessions": row["sessions"], "episodes": row["episodes"],
            "strategy_compounded_return": strategy_return,
            "baseline_compounded_return": baseline_return,
            "relative_wealth_vs_baseline": relative,
            "relative_wealth_vs_baseline_at_20bps": relative_20,
            "beat_baseline": relative > 0,
            "positive_at_20bps_vs_baseline": relative_20 is not None and relative_20 > 0,
            "conditional_sequence_max_drawdown": _f(row["conditional_sequence_max_drawdown"]),
            "positive_episode_rate": _f(row["positive_episode_rate"]),
        })
    return output


def build_tactical_horizon_comparisons(
    config, event_rows, state_labels, daily, baseline_strategy,
):
    primary = float(config.primary_cost_bps)
    labels = {row["date"]: row for row in state_labels}
    state_age = {}
    age, previous = 0, None
    for row in state_labels:
        current = row["core_state"]
        age = 0 if current != previous else age + 1
        state_age[row["date"]] = age
        previous = current
    baseline_daily = [row for row in daily if row["strategy"] == baseline_strategy
                      and float(row["cost_bps"]) == primary]
    equity = {row["date"]: float(row["equity"]) for row in baseline_daily}
    dates = [row["date"] for row in baseline_daily]
    date_index = {day: index for index, day in enumerate(dates)}
    comparisons = []
    for row in event_rows:
        if float(row["cost_bps"]) != primary or strategy_role(row["strategy"]) != "tactical_opportunity":
            continue
        entry, index = row["entry_date"], date_index.get(row["entry_date"])
        label = labels.get(entry, {})
        for horizon in HORIZONS:
            gross = _f(row.get(f"exit_{horizon}d_return"))
            if gross is None or index is None or index + horizon >= len(dates):
                continue
            end = dates[index + horizon]
            baseline_return = equity[end] / equity[entry] - 1
            tactical_net = ((1 + gross) * (1 - primary / 10000.0)
                            / (1 + primary / 10000.0) - 1)
            comparisons.append({
                "strategy": row["strategy"], "baseline_strategy": baseline_strategy,
                "signal_date": row["signal_date"], "entry_date": entry,
                "comparison_end_date": end, "horizon_sessions": horizon,
                "core_state": label.get("core_state"),
                "state_age_sessions_at_entry": state_age.get(entry),
                "state_changed_at_entry": label.get("state_changed"),
                "pending_core_state_at_entry": label.get("pending_core_state"),
                "pending_confirmation_sessions_at_entry": label.get("pending_confirmation_sessions"),
                "tactical_net_return": tactical_net, "baseline_return": baseline_return,
                "incremental_return_vs_baseline": tactical_net - baseline_return,
                "tactical_beat_baseline": tactical_net > baseline_return,
            })
    grouped = defaultdict(list)
    for row in comparisons:
        grouped[(row["strategy"], row["horizon_sessions"], "ALL")].append(row)
        grouped[(row["strategy"], row["horizon_sessions"], row["core_state"] or "UNKNOWN")].append(row)
    summary = []
    for (strategy, horizon, state), rows in sorted(grouped.items()):
        incremental = [row["incremental_return_vs_baseline"] for row in rows]
        ages = [row["state_age_sessions_at_entry"] for row in rows
                if row["state_age_sessions_at_entry"] is not None]
        summary.append({
            "strategy": strategy, "baseline_strategy": baseline_strategy,
            "horizon_sessions": horizon, "core_state": state, "events": len(rows),
            "mean_tactical_net_return": statistics.fmean(row["tactical_net_return"] for row in rows),
            "mean_baseline_return": statistics.fmean(row["baseline_return"] for row in rows),
            "mean_incremental_return_vs_baseline": statistics.fmean(incremental),
            "median_incremental_return_vs_baseline": statistics.median(incremental),
            "beat_baseline_rate": sum(value > 0 for value in incremental) / len(incremental),
            "median_state_age_sessions_at_entry": statistics.median(ages) if ages else None,
            "entries_during_pending_transition": sum(bool(row["pending_core_state_at_entry"]) for row in rows),
            "calendar_years": len({row["entry_date"][:4] for row in rows}),
            "conditional_horizon_candidate": bool(
                state != "ALL" and len(rows) >= 20 and statistics.fmean(incremental) > 0
                and sum(value > 0 for value in incremental) / len(incremental) >= .55
                and len({row["entry_date"][:4] for row in rows}) >= 3
            ),
        })
    return comparisons, summary
