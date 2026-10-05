"""Causal, event-centered diagnostics for short downside opportunities."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict


HORIZONS = (1, 3, 5, 10)
EVENT_DEFINITIONS = {
    "FRESH_20D_LOW": "Prior close is below every preceding close in the 20-session window.",
    "DRAWDOWN_ACCELERATION": "Prior drawdown is at most -5% and deepened by at least 2 percentage points in five sessions.",
    "FAILED_REBOUND": "Below the 200-day mean, a prior five-session rise of at least 2% is followed by a five-session decline of at least 2%.",
    "BREADTH_VOL_JOINT_DOWNSIDE": "Prior breadth is narrowing, volatility is rising, and the causal 20-day trend is negative.",
}


def _compound(values: list[float]) -> float:
    return math.prod(1.0 + value for value in values) - 1.0


def build_downside_event_diagnostics(
    daily: list[dict], conditions: dict[str, dict], primary_cost_bps: float,
    benchmark_strategy: str = "SPY_BUY_HOLD", cluster_gap_sessions: int = 10,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Build causal event observations, summaries, and overlap counts."""
    rows = sorted(
        (row for row in daily if row["strategy"] == benchmark_strategy
         and float(row["cost_bps"]) == float(primary_cost_bps)
         and row["date"] in conditions), key=lambda row: row["date"],
    )
    dates = [row["date"] for row in rows]
    equity = [float(row["equity"]) for row in rows]
    returns = [None] + [equity[index] / equity[index - 1] - 1.0
                        for index in range(1, len(equity))]
    observations = []
    last_event_index = {}
    cluster_number = defaultdict(int)
    for index, day in enumerate(dates):
        if index < 21:
            continue
        prior = index - 1
        prior_close = equity[prior]
        prior_peak = max(equity[:index])
        prior_drawdown = prior_close / prior_peak - 1.0
        five_day_prior_drawdown = equity[max(0, prior - 5)] / max(equity[:max(1, prior - 4)]) - 1.0
        recent_5d = prior_close / equity[prior - 5] - 1.0
        preceding_5d = equity[prior - 5] / equity[prior - 10] - 1.0
        source = conditions[day]
        event_flags = {
            "FRESH_20D_LOW": prior_close < min(equity[prior - 20:prior]),
            "DRAWDOWN_ACCELERATION": (
                prior_drawdown <= -0.05
                and prior_drawdown - five_day_prior_drawdown <= -0.02
            ),
            "FAILED_REBOUND": (
                float(source.get("trend_200d_distance") or 0.0) < 0
                and preceding_5d >= 0.02 and recent_5d <= -0.02
            ),
            "BREADTH_VOL_JOINT_DOWNSIDE": (
                float(source.get("breadth_50d_change_5d") or 0.0) < 0
                and float(source.get("volatility_change_5d") or 0.0) > 0
                and float(source.get("trend_20d_return") or 0.0) < 0
            ),
        }
        active = [name for name, enabled in event_flags.items() if enabled]
        for event_type in active:
            previous = last_event_index.get(event_type)
            if previous is None or index - previous > cluster_gap_sessions:
                cluster_number[event_type] += 1
            last_event_index[event_type] = index
            row = {
                "date": day, "event_type": event_type,
                "event_cluster_id": f"{event_type}:{cluster_number[event_type]:04d}",
                "simultaneous_event_count": len(active),
                "simultaneous_events": "|".join(active),
                "prior_drawdown": prior_drawdown,
                "recent_5d_return": recent_5d,
                "preceding_5d_return": preceding_5d,
                "diagnostic_only": True,
            }
            for horizon in HORIZONS:
                window = returns[index:index + horizon]
                row[f"forward_{horizon}d_spy_return"] = (
                    _compound(window) if len(window) == horizon
                    and all(value is not None for value in window) else None
                )
            observations.append(row)

    grouped = defaultdict(list)
    for row in observations:
        for horizon in HORIZONS:
            value = row[f"forward_{horizon}d_spy_return"]
            if value is not None:
                grouped[(row["event_type"], horizon)].append((row, value))
    summaries = []
    for (event_type, horizon), values_with_rows in sorted(grouped.items()):
        values = [value for _, value in values_with_rows]
        summaries.append({
            "event_type": event_type,
            "event_definition": EVENT_DEFINITIONS[event_type],
            "horizon_sessions": horizon,
            "observations": len(values),
            "independent_event_clusters": len({row["event_cluster_id"] for row, _ in values_with_rows}),
            "mean_forward_spy_return": statistics.fmean(values),
            "median_forward_spy_return": statistics.median(values),
            "negative_forward_return_rate": sum(value < 0 for value in values) / len(values),
            "diagnostic_only": True,
        })

    overlap = defaultdict(int)
    for row in observations:
        overlap[(row["simultaneous_events"], row["simultaneous_event_count"])] += 1
    overlap_rows = [
        {"simultaneous_events": events, "simultaneous_event_count": count,
         "sessions": sessions, "diagnostic_only": True}
        for (events, count), sessions in sorted(overlap.items())
    ]
    return observations, summaries, overlap_rows
