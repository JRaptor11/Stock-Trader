"""Deep, role-specific diagnostics without constructing a strategy router."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict

from research.market_state_validation import (
    _benjamini_hochberg, _episode_bootstrap, _mean_test,
)
from research.market_state_episodes import causal_state_labels, state_episodes
from research.role_aware_evidence import strategy_role


HORIZONS = (1, 2, 3, 5, 10)
CONFIRMATION_WINDOWS = (1, 3, 5, 10)


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


def build_defensive_baseline_comparisons(
    config, state_periods, state_costs, baseline_strategy,
):
    roles = {name: strategy_role(name) for name in config.strategy_names}
    primary_index = {
        (row["period"], row["core_state"], row["strategy"]): row
        for row in state_periods
        if float(row["cost_bps"]) == float(config.primary_cost_bps)
    }
    cost_index = {
        (row["period"], row["core_state"], row["strategy"], float(row["cost_bps"])): row
        for row in state_costs
    }
    output = []
    for row in state_periods:
        strategy = row["strategy"]
        cost = float(row["cost_bps"])
        if cost != float(config.primary_cost_bps) or roles.get(strategy) != "defensive_override":
            continue
        baseline = primary_index.get((row["period"], row["core_state"], baseline_strategy))
        baseline_20 = cost_index.get(
            (row["period"], row["core_state"], baseline_strategy, 20.0)
        )
        strategy_20 = cost_index.get(
            (row["period"], row["core_state"], strategy, 20.0)
        )
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
    state_episode = {}
    age, previous, episode = 0, None, 0
    for row in state_labels:
        current = row["core_state"]
        if current != previous:
            episode += 1
        age = 0 if current != previous else age + 1
        state_age[row["date"]] = age
        state_episode[row["date"]] = episode
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
                "state_episode_id": state_episode.get(entry),
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


def build_locked_tactical_validation(config, comparisons, hypotheses):
    """Evaluate predeclared state/horizon ideas without selecting winners again."""
    periods = [("full", None, None)]
    if config.discovery_end_date and config.holdout_start_date:
        periods += [
            ("discovery", None, config.discovery_end_date),
            ("holdout", config.holdout_start_date, None),
        ]
    output = []
    for period, start, end in periods:
        for hypothesis in hypotheses:
            transition_phase = hypothesis.get("transition_phase", "all")
            rows = [row for row in comparisons
                    if row["strategy"] == hypothesis["strategy"]
                    and row["core_state"] == hypothesis["core_state"]
                    and int(row["horizon_sessions"]) == int(hypothesis["horizon_sessions"])
                    and (start is None or row["entry_date"] >= start)
                    and (end is None or row["entry_date"] <= end)
                    and (transition_phase == "all"
                         or (transition_phase == "pending") == bool(row["pending_core_state_at_entry"]))]
            values = [float(row["incremental_return_vs_baseline"]) for row in rows]
            episode_groups = defaultdict(list)
            year_groups = defaultdict(list)
            for row in rows:
                episode_groups[row["state_episode_id"]].append(
                    float(row["incremental_return_vs_baseline"])
                )
                year_groups[row["entry_date"][:4]].append(
                    float(row["incremental_return_vs_baseline"])
                )
            episode_values = [statistics.fmean(group) for group in episode_groups.values()]
            year_values = [statistics.fmean(group) for group in year_groups.values()]
            boot = _episode_bootstrap(episode_values)
            output.append({
                "hypothesis_id": hypothesis["hypothesis_id"], "period": period,
                "strategy": hypothesis["strategy"], "core_state": hypothesis["core_state"],
                "horizon_sessions": int(hypothesis["horizon_sessions"]),
                "transition_phase": transition_phase, "events": len(values),
                "state_episodes": len(episode_values), "calendar_years": len(year_values),
                "mean_incremental_return_vs_baseline": statistics.fmean(values) if values else None,
                "median_incremental_return_vs_baseline": statistics.median(values) if values else None,
                "beat_baseline_rate": sum(value > 0 for value in values) / len(values) if values else None,
                "episode_mean_incremental_return": statistics.fmean(episode_values) if episode_values else None,
                "episode_ci_95_low": boot["ci_low"], "episode_ci_95_high": boot["ci_high"],
                "bootstrap_probability_positive": boot["probability_positive"],
                "raw_p_value": _mean_test(episode_values),
                "positive_year_rate": sum(value > 0 for value in year_values) / len(year_values) if year_values else None,
                "worst_year_mean_incremental_return": min(year_values) if year_values else None,
                "additional_20bps_round_trip_stress": (
                    statistics.fmean(values) - .002 if values else None
                ),
                "sample_sufficient": len(values) >= 20 and len(episode_values) >= 5 and len(year_values) >= 3,
            })
        _benjamini_hochberg([row for row in output if row["period"] == period])
    for row in output:
        row["passes_locked_gate"] = bool(
            row["sample_sufficient"] and row["passes_fdr_05"]
            and row["episode_ci_95_low"] is not None and row["episode_ci_95_low"] > 0
            and row["positive_year_rate"] is not None and row["positive_year_rate"] >= .60
            and row["additional_20bps_round_trip_stress"] is not None
            and row["additional_20bps_round_trip_stress"] > 0
        )
    return output


def build_defensive_distinctness(config, daily, trades, state_labels):
    """Measure whether defensive names actually create distinct behavior."""
    primary = float(config.primary_cost_bps)
    defensive = sorted(name for name in config.strategy_names
                       if strategy_role(name) == "defensive_override")
    labels = {row["date"]: row["core_state"] for row in state_labels}
    equity = defaultdict(dict)
    for row in daily:
        if float(row["cost_bps"]) == primary and row["strategy"] in defensive:
            equity[row["strategy"]][row["date"]] = float(row["equity"])
    returns = defaultdict(dict)
    for strategy, values in equity.items():
        previous = None
        for day, value in sorted(values.items()):
            if previous not in (None, 0.0):
                returns[strategy][day] = value / previous - 1
            previous = value
    trade_days = defaultdict(set)
    for row in trades:
        if float(row["cost_bps"]) == primary and row["strategy"] in defensive:
            trade_days[row["strategy"]].add(row["date"])
    output = []
    for left_index, left in enumerate(defensive):
        for right in defensive[left_index + 1:]:
            common = sorted(set(returns[left]).intersection(returns[right]))
            for state in ["ALL", *sorted(set(labels.values()))]:
                dates = [day for day in common if state == "ALL" or labels.get(day) == state]
                if not dates:
                    continue
                left_values = [returns[left][day] for day in dates]
                right_values = [returns[right][day] for day in dates]
                differences = [abs(a - b) for a, b in zip(left_values, right_values)]
                union = (trade_days[left] | trade_days[right]) & set(dates)
                intersection = trade_days[left] & trade_days[right] & set(dates)
                trade_jaccard = len(intersection) / len(union) if union else 1.0
                correlation = None
                if len(dates) > 1 and statistics.stdev(left_values) and statistics.stdev(right_values):
                    correlation = statistics.correlation(left_values, right_values)
                identical_rate = sum(value <= 1e-12 for value in differences) / len(differences)
                equivalent_rate = sum(value <= .0001 for value in differences) / len(differences)
                mean_difference = statistics.fmean(differences)
                output.append({
                    "core_state": state, "left_strategy": left, "right_strategy": right,
                    "sessions": len(dates), "return_correlation": correlation,
                    "identical_daily_return_rate": identical_rate,
                    "economically_equivalent_daily_return_rate_1bp": equivalent_rate,
                    "mean_absolute_daily_return_difference": mean_difference,
                    "shared_trade_day_jaccard": trade_jaccard,
                    "behaviorally_indistinguishable": bool(
                        mean_difference <= .0001
                        and ((correlation is not None and correlation >= .999)
                             or trade_jaccard >= .95)
                    ),
                })
    return output


def build_baseline_era_recurrence(config, daily, state_labels, benchmark_strategy="SPY_BUY_HOLD"):
    """Evaluate baseline-state relationships in fixed, non-overlapping eras."""
    primary = float(config.primary_cost_bps)
    eras = (("2019_2020", "2019-01-01", "2020-12-31"),
            ("2021_2022", "2021-01-01", "2022-12-31"),
            ("2023_2024", "2023-01-01", "2024-12-31"),
            ("2025_plus", "2025-01-01", "9999-12-31"))
    labels = {row["date"]: row["core_state"] for row in state_labels}
    returns = defaultdict(dict)
    for strategy in config.strategy_names:
        rows = sorted((row for row in daily if row["strategy"] == strategy
                       and float(row["cost_bps"]) == primary), key=lambda row: row["date"])
        previous = None
        for row in rows:
            value = float(row["equity"])
            if previous not in (None, 0.0):
                returns[strategy][row["date"]] = value / previous - 1
            previous = value
    details = []
    baselines = [name for name in config.strategy_names if strategy_role(name) == "baseline_candidate"]
    for era, start, end in eras:
        for state in sorted(set(labels.values())):
            dates = [day for day, value in labels.items() if value == state and start <= day <= end]
            for strategy in baselines:
                usable = [day for day in dates if day in returns[strategy] and day in returns[benchmark_strategy]]
                if not usable:
                    continue
                strategy_return = math.prod(1 + returns[strategy][day] for day in usable) - 1
                benchmark_return = math.prod(1 + returns[benchmark_strategy][day] for day in usable) - 1
                details.append({
                    "era": era, "core_state": state, "strategy": strategy,
                    "sessions": len(usable), "strategy_compounded_return": strategy_return,
                    "benchmark_compounded_return": benchmark_return,
                    "relative_wealth_vs_benchmark": (1 + strategy_return) / (1 + benchmark_return) - 1,
                    "era_eligible": len(usable) >= 20,
                })
    grouped = defaultdict(list)
    for row in details:
        if row["era_eligible"]:
            grouped[(row["strategy"], row["core_state"])].append(row)
    summary = []
    for (strategy, state), rows in sorted(grouped.items()):
        values = [row["relative_wealth_vs_benchmark"] for row in rows]
        summary.append({
            "strategy": strategy, "core_state": state, "eligible_eras": len(rows),
            "positive_eras": sum(value > 0 for value in values),
            "era_win_rate": sum(value > 0 for value in values) / len(values),
            "mean_era_relative_wealth": statistics.fmean(values),
            "median_era_relative_wealth": statistics.median(values),
            "worst_era_relative_wealth": min(values),
            "recurs_across_eras": len(rows) >= 3 and sum(value > 0 for value in values) / len(values) >= 2 / 3,
        })
    return details, summary


def _daily_returns(daily, cost_bps):
    equity = defaultdict(dict)
    for row in daily:
        if float(row["cost_bps"]) == float(cost_bps):
            equity[row["strategy"]][row["date"]] = float(row["equity"])
    returns = defaultdict(dict)
    for strategy, values in equity.items():
        previous = None
        for day, value in sorted(values.items()):
            if previous not in (None, 0.0):
                returns[strategy][day] = value / previous - 1.0
            previous = value
    return returns


def build_baseline_confirmation_sensitivity(
    config, daily, conditions, benchmark_strategy="SPY_BUY_HOLD",
    confirmation_windows=CONFIRMATION_WINDOWS,
):
    """Re-label the same history with fixed causal confirmation delays.

    This is a diagnostic sensitivity analysis. It never selects a delay or
    authorizes routing. Whole episodes remain the independent evidence unit.
    """
    returns = _daily_returns(daily, config.primary_cost_bps)
    baselines = [name for name in config.strategy_names
                 if strategy_role(name) == "baseline_candidate"]
    output = []
    for confirmation in confirmation_windows:
        labels = causal_state_labels(conditions, confirmation_sessions=confirmation)
        episodes, episode_map = state_episodes(labels)
        states = sorted({row["core_state"] for row in labels.values()})
        for state in states:
            state_dates = [day for day, row in labels.items() if row["core_state"] == state]
            for strategy in baselines:
                usable = [day for day in state_dates
                          if day in returns[strategy] and day in returns[benchmark_strategy]]
                if not usable:
                    continue
                episode_values = defaultdict(list)
                for day in usable:
                    episode_values[episode_map[day]].append(
                        (1.0 + returns[strategy][day])
                        / (1.0 + returns[benchmark_strategy][day]) - 1.0
                    )
                episode_relative = [math.prod(1.0 + value for value in values) - 1.0
                                    for values in episode_values.values()]
                relative = math.prod(
                    (1.0 + returns[strategy][day])
                    / (1.0 + returns[benchmark_strategy][day]) for day in usable
                ) - 1.0
                boot = _episode_bootstrap(episode_relative)
                pending = sum(bool(labels[day].get("pending_core_state")) for day in usable)
                output.append({
                    "confirmation_sessions": confirmation, "core_state": state,
                    "strategy": strategy, "benchmark_strategy": benchmark_strategy,
                    "sessions": len(usable), "episodes": len(episode_relative),
                    "pending_sessions": pending,
                    "relative_wealth_vs_benchmark": relative,
                    "mean_episode_relative_wealth": statistics.fmean(episode_relative),
                    "median_episode_relative_wealth": statistics.median(episode_relative),
                    "worst_episode_relative_wealth": min(episode_relative),
                    "positive_episode_rate": sum(value > 0 for value in episode_relative)
                                             / len(episode_relative),
                    "episode_ci_95_low": boot["ci_low"],
                    "episode_ci_95_high": boot["ci_high"],
                    "sample_sufficient": len(usable) >= 30 and len(episode_relative) >= 5,
                    "diagnostic_only": True,
                })
    return output


def build_state_transition_timing(
    config, daily, conditions, benchmark_strategy="SPY_BUY_HOLD",
    confirmation_windows=CONFIRMATION_WINDOWS,
):
    """Measure false raw transitions and performance after causal confirmation."""
    returns = _daily_returns(daily, config.primary_cost_bps)
    baselines = [name for name in config.strategy_names
                 if strategy_role(name) == "baseline_candidate"]
    raw = causal_state_labels(conditions, confirmation_sessions=1)
    days = sorted(raw)
    runs = []
    start = 0
    for index in range(1, len(days) + 1):
        if index == len(days) or raw[days[index]]["core_state"] != raw[days[start]]["core_state"]:
            runs.append((start, index - 1, raw[days[start]]["core_state"]))
            start = index
    output = []
    for confirmation in confirmation_windows:
        for run_index, (start, end, state) in enumerate(runs):
            if run_index == 0:
                continue
            prior_state = runs[run_index - 1][2]
            length = end - start + 1
            confirmed = length >= confirmation
            confirmation_index = start + confirmation - 1 if confirmed else None
            for strategy in baselines:
                for horizon in HORIZONS:
                    relative = None
                    end_day = None
                    if confirmed and confirmation_index + horizon < len(days):
                        window = days[confirmation_index + 1:confirmation_index + horizon + 1]
                        usable = [day for day in window
                                  if day in returns[strategy] and day in returns[benchmark_strategy]]
                        if len(usable) == horizon:
                            relative = math.prod(
                                (1.0 + returns[strategy][day])
                                / (1.0 + returns[benchmark_strategy][day]) for day in usable
                            ) - 1.0
                            end_day = usable[-1]
                    output.append({
                        "confirmation_sessions": confirmation,
                        "transition_id": f"RAW:{run_index:05d}",
                        "prior_core_state": prior_state, "proposed_core_state": state,
                        "proposal_date": days[start], "raw_run_sessions": length,
                        "confirmed": confirmed, "false_transition": not confirmed,
                        "confirmation_date": days[confirmation_index] if confirmed else None,
                        "strategy": strategy, "benchmark_strategy": benchmark_strategy,
                        "horizon_sessions": horizon, "comparison_end_date": end_day,
                        "relative_wealth_vs_benchmark": relative,
                        "diagnostic_only": True,
                    })
    return output


def build_generation_candidate_map(
    config, confirmation_rows, era_recurrence, tactical_validation,
    defensive_comparisons,
):
    """Create an auditable research map without selecting or routing strategies."""
    rows = []
    recurrence = {(row["strategy"], row["core_state"]): row for row in era_recurrence}
    grouped = defaultdict(list)
    for row in confirmation_rows:
        grouped[(row["strategy"], row["core_state"])].append(row)
    for (strategy, state), values in sorted(grouped.items()):
        eligible = [row for row in values if row["sample_sufficient"]]
        positive = [row for row in eligible if row["mean_episode_relative_wealth"] > 0]
        recurring = recurrence.get((strategy, state), {})
        rows.append({
            "evidence_role": "baseline_candidate", "core_state_or_event": state,
            "strategy": strategy, "challenger_retained": True,
            "confirmation_windows_tested": len(values),
            "eligible_confirmation_windows": len(eligible),
            "positive_confirmation_windows": len(positive),
            "confirmation_robust": bool(eligible and len(positive) == len(eligible)),
            "recurs_across_fixed_eras": bool(recurring.get("recurs_across_eras", False)),
            "status": "RESEARCH_CANDIDATE",
            "routing_or_promotion_authorized": False,
        })
    for row in tactical_validation:
        if row["period"] != "full":
            continue
        rows.append({
            "evidence_role": "tactical_opportunity",
            "core_state_or_event": row["core_state"], "strategy": row["strategy"],
            "challenger_retained": True, "confirmation_windows_tested": None,
            "eligible_confirmation_windows": None, "positive_confirmation_windows": None,
            "confirmation_robust": None, "recurs_across_fixed_eras": None,
            "status": "LOCKED_GATE_PASS" if row["passes_locked_gate"] else "INSUFFICIENT_OR_FAILED_LOCKED_GATE",
            "routing_or_promotion_authorized": False,
        })
    seen_defensive = set()
    for row in defensive_comparisons:
        key = (row["strategy"], row["core_state"])
        if row["period"] != "full" or key in seen_defensive:
            continue
        seen_defensive.add(key)
        rows.append({
            "evidence_role": "defensive_override", "core_state_or_event": row["core_state"],
            "strategy": row["strategy"], "challenger_retained": True,
            "confirmation_windows_tested": None, "eligible_confirmation_windows": None,
            "positive_confirmation_windows": None, "confirmation_robust": None,
            "recurs_across_fixed_eras": None,
            "status": "BEAT_BASELINE" if row["beat_baseline"] else "DID_NOT_BEAT_BASELINE",
            "routing_or_promotion_authorized": False,
        })
    return rows
