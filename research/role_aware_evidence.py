"""Unified evidence for baseline, defensive, tactical, and hybrid strategies."""

from __future__ import annotations

import statistics
from collections import defaultdict


TACTICAL = {
    "SECTOR_SHORT_TERM_REVERSAL", "SECTOR_PRICE_BREAKOUT_20D", "MARKET_DIP_REBOUND_1D",
    "VOLATILITY_CONTRACTION_BREAKOUT", "DONCHIAN_TREND_BREAKOUT",
    "SECTOR_MOMENTUM_ACCELERATION", "BREADTH_THRUST_RECOVERY",
    "OVERSOLD_TREND_REBOUND", "TREND_PULLBACK_REBOUND", "FAILED_BREAKDOWN_RECOVERY",
    "RELATIVE_STRENGTH_BREAKOUT", "CONFIRMED_CRASH_RECOVERY",
    "VOLUME_EXPANSION_BREAKOUT", "VOLATILITY_ADJUSTED_ACCELERATION_BREAKOUT",
    "OVERNIGHT_GAP_CONTINUATION", "CROSS_SECTIONAL_ABNORMAL_RETURN_BREAKOUT",
    "SECTOR_PARTICIPATION_BREAKOUT", "MARKET_CONFIRMED_SECTOR_BREAKOUT",
}
DEFENSIVE = {
    "BREADTH_DETERIORATION_DEFENSIVE", "EQUITY_TREND_DEFENSIVE", "DRAWDOWN_BRAKE",
    "VOLATILITY_SHOCK_DEFENSIVE", "DEFENSIVE_ASSET_BREAKOUT",
    "TREND_VOLATILITY_SCALED_EQUITY", "DEFENSIVE_TREND_PERSISTENCE",
    "BREADTH_DIVERGENCE_DEFENSIVE",
}
HYBRID_CONTROLS = {"REGIME_BALANCED", "REGIME_ROUTED_SECTOR", "REGIME_MULTI_SLEEVE"}


def strategy_role(name: str) -> str:
    if name in TACTICAL:
        return "tactical_opportunity"
    if name in DEFENSIVE:
        return "defensive_override"
    if name in HYBRID_CONTROLS:
        return "hybrid_control"
    if name == "SPY_BUY_HOLD":
        return "benchmark"
    return "baseline_candidate"


def _f(value):
    return None if value in (None, "") else float(value)


def build_role_aware_evidence(
    config, scorecards, period_scorecards, state_periods, state_survival,
    event_rows, state_labels, daily, concept_families, baseline_strategy,
):
    """Build independent-strategy evidence and baseline-relative tactical outcomes."""
    primary = float(config.primary_cost_bps)
    overall = {row["strategy"]: row for row in scorecards if float(row["cost_bps"]) == primary}
    periods = {(row["strategy"], row["period"]): row for row in period_scorecards
               if float(row["cost_bps"]) == primary}
    survival = defaultdict(list)
    for row in state_survival:
        survival[row["strategy"]].append(row)
    states = defaultdict(list)
    for row in state_periods:
        if row["period"] == "holdout" and float(row["cost_bps"]) == primary:
            states[row["strategy"]].append(row)

    matrix = []
    for strategy in config.strategy_names:
        state_rows = states[strategy]
        supported = [row for row in survival[strategy]
                     if row["status"] not in {"INSUFFICIENT_EVIDENCE", "FAILED_STATISTICAL_GATES"}]
        best = max(state_rows, key=lambda row: _f(row["relative_wealth_vs_spy"]) or -10.0,
                   default=None)
        full, holdout = periods.get((strategy, "full"), {}), periods.get((strategy, "holdout"), {})
        row = overall.get(strategy, {})
        matrix.append({
            "strategy": strategy, "role": strategy_role(strategy),
            "concept_family": concept_families.get(strategy),
            "primary_cost_bps": primary, "full_total_return": _f(full.get("total_return")),
            "holdout_total_return": _f(holdout.get("total_return")),
            "full_sharpe": _f(row.get("sharpe")), "full_max_drawdown": _f(row.get("max_drawdown")),
            "turnover": _f(row.get("turnover")), "trade_count": row.get("trade_count"),
            "states_with_surviving_evidence": len(supported),
            "best_holdout_state": best.get("core_state") if best else None,
            "best_holdout_state_relative_wealth_vs_spy": _f(best.get("relative_wealth_vs_spy")) if best else None,
            "routing_eligible": any(str(item.get("routing_eligible")).lower() == "true"
                                    for item in survival[strategy]),
        })

    date_state = {row["date"]: row["core_state"] for row in state_labels}
    baseline_daily = [row for row in daily if row["strategy"] == baseline_strategy
                      and float(row["cost_bps"]) == primary]
    baseline_equity = {row["date"]: float(row["equity"]) for row in baseline_daily}
    baseline_dates = [row["date"] for row in baseline_daily]
    baseline_index = {day: index for index, day in enumerate(baseline_dates)}
    comparisons = []
    for row in event_rows:
        if float(row["cost_bps"]) != primary or strategy_role(row["strategy"]) != "tactical_opportunity":
            continue
        entry, event_return = row["entry_date"], _f(row.get("exit_5d_return"))
        index = baseline_index.get(entry)
        if event_return is None or index is None or index + 5 >= len(baseline_dates):
            continue
        end = baseline_dates[index + 5]
        baseline_return = baseline_equity[end] / baseline_equity[entry] - 1.0
        # Apply a round-trip cost to the fixed-horizon tactical result.
        tactical_net = (1.0 + event_return) * (1.0 - primary / 10000.0) / (1.0 + primary / 10000.0) - 1.0
        comparisons.append({
            "strategy": row["strategy"], "role": "tactical_opportunity",
            "baseline_strategy": baseline_strategy, "entry_date": entry,
            "comparison_end_date": end, "core_state": date_state.get(entry),
            "tactical_net_5d_return": tactical_net, "baseline_5d_return": baseline_return,
            "incremental_return_vs_baseline": tactical_net - baseline_return,
            "tactical_beat_baseline": tactical_net > baseline_return,
        })
    grouped = defaultdict(list)
    for row in comparisons:
        grouped[(row["strategy"], "ALL")].append(row)
        grouped[(row["strategy"], row["core_state"] or "UNKNOWN")].append(row)
    tactical_summary = []
    for (strategy, state), rows in sorted(grouped.items()):
        incremental = [row["incremental_return_vs_baseline"] for row in rows]
        tactical_summary.append({
            "strategy": strategy, "baseline_strategy": baseline_strategy,
            "core_state": state, "events": len(rows),
            "mean_tactical_net_5d_return": statistics.fmean(row["tactical_net_5d_return"] for row in rows),
            "mean_baseline_5d_return": statistics.fmean(row["baseline_5d_return"] for row in rows),
            "mean_incremental_return_vs_baseline": statistics.fmean(incremental),
            "median_incremental_return_vs_baseline": statistics.median(incremental),
            "beat_baseline_rate": sum(value > 0 for value in incremental) / len(incremental),
            "calendar_years": len({row["entry_date"][:4] for row in rows}),
            "conditional_override_candidate": bool(
                state != "ALL" and len(rows) >= 20
                and statistics.fmean(incremental) > 0
                and sum(value > 0 for value in incremental) / len(incremental) >= .55
                and len({row["entry_date"][:4] for row in rows}) >= 3
            ),
        })
    return matrix, comparisons, tactical_summary
