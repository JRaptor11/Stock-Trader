"""Causal state-entry and persistence diagnostics for long-term research.

Forward returns and episode endings are outcomes used only for evaluation.  Every
candidate modifier is known before the evaluated session begins.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict

from research.market_state_episodes import causal_state_labels, project_state_labels, state_episodes


HORIZONS = (1, 3, 5, 10, 20)


def _bucket(value, negative: str, positive: str, deadband: float = 0.0) -> str:
    if value is None:
        return "UNAVAILABLE"
    value = float(value)
    if value < -deadband:
        return negative
    if value > deadband:
        return positive
    return "FLAT"


def _compound(values: list[float]) -> float:
    return math.prod(1.0 + value for value in values) - 1.0


def build_state_actionability_diagnostics(
    daily: list[dict],
    conditions: dict[str, dict],
    primary_cost_bps: float,
    benchmark_strategy: str = "SPY_BUY_HOLD",
    target_trend_state: str = "BEAR_DETERIORATING",
) -> tuple[list[dict], list[dict], list[dict]]:
    """Return session outcomes, causal modifier summaries, and episode segments."""
    labels = project_state_labels(causal_state_labels(conditions), "trend")
    episodes, episode_for_date = state_episodes(labels)
    episode_by_id = {row["episode_id"]: row for row in episodes}

    benchmark_rows = sorted(
        (row for row in daily
         if row["strategy"] == benchmark_strategy
         and float(row["cost_bps"]) == float(primary_cost_bps)
         and row["date"] in labels),
        key=lambda row: row["date"],
    )
    dates = [row["date"] for row in benchmark_rows]
    equity = [float(row["equity"]) for row in benchmark_rows]
    returns = [None] + [equity[index] / equity[index - 1] - 1.0
                        for index in range(1, len(equity))]

    episode_age = defaultdict(int)
    peak_before = None
    prior_raw_trend = None
    session_rows = []
    for index, day in enumerate(dates):
        label = labels[day]
        episode_id = episode_for_date[day]
        episode_age[episode_id] += 1
        age = episode_age[episode_id]
        current_equity = equity[index]
        drawdown_before = None if peak_before is None else equity[index - 1] / peak_before - 1.0
        if index:
            peak_before = max(peak_before or equity[index - 1], equity[index - 1])
            drawdown_before = equity[index - 1] / peak_before - 1.0
        else:
            peak_before = current_equity

        raw_onset = (
            label["raw_trend_state"] == target_trend_state
            and label["trend_state"] != target_trend_state
            and prior_raw_trend != target_trend_state
        )
        confirmed = label["trend_state"] == target_trend_state
        prior_raw_trend = label["raw_trend_state"]
        if not (raw_onset or confirmed):
            continue
        phase = "RAW_ONSET_UNCONFIRMED"
        if confirmed:
            if label["state_changed"]:
                phase = "CONFIRMED_ENTRY"
            elif age <= 3:
                phase = "CONFIRMED_EARLY"
            else:
                phase = "CONFIRMED_PERSISTENT"
        source = conditions.get(day, {})
        episode = episode_by_id[episode_id]
        row = {
            "date": day,
            "target_trend_state": target_trend_state,
            "raw_trend_state": label["raw_trend_state"],
            "confirmed_trend_state": label["trend_state"],
            "phase": phase,
            "confirmed_episode_id": episode_id if confirmed else None,
            "confirmed_episode_age_sessions": age if confirmed else None,
            "confirmed_episode_end_date": episode["end_date"] if confirmed else None,
            "episode_end_is_hindsight_only": True,
            "trend_20d_direction": _bucket(source.get("trend_20d_return"), "NEGATIVE", "POSITIVE"),
            "trend_acceleration_direction": _bucket(source.get("trend_acceleration_5d"), "FALLING", "RISING"),
            "volatility_change_direction": _bucket(source.get("volatility_change_5d"), "FALLING", "RISING"),
            "breadth_change_direction": _bucket(source.get("breadth_50d_change_5d"), "NARROWING", "BROADENING"),
            "prior_drawdown_bucket": (
                "UNAVAILABLE" if drawdown_before is None else
                "BELOW_MINUS_10" if drawdown_before <= -0.10 else
                "MINUS_5_TO_10" if drawdown_before <= -0.05 else
                "ABOVE_MINUS_5"
            ),
            "diagnostic_only": True,
        }
        for horizon in HORIZONS:
            window = returns[index:index + horizon]
            row[f"forward_{horizon}d_spy_return"] = (
                _compound(window) if len(window) == horizon and all(value is not None for value in window)
                else None
            )
        session_rows.append(row)

    dimensions = (
        "phase", "trend_20d_direction", "trend_acceleration_direction",
        "volatility_change_direction", "breadth_change_direction", "prior_drawdown_bucket",
    )
    grouped = defaultdict(list)
    for row in session_rows:
        if row["confirmed_trend_state"] != target_trend_state:
            continue
        for dimension in dimensions:
            for horizon in HORIZONS:
                value = row[f"forward_{horizon}d_spy_return"]
                if value is not None:
                    grouped[(dimension, row[dimension], horizon)].append((row, value))
    summaries = []
    for (dimension, bucket, horizon), observations in sorted(grouped.items()):
        values = [value for _, value in observations]
        summaries.append({
            "target_trend_state": target_trend_state,
            "causal_dimension": dimension,
            "causal_bucket": bucket,
            "horizon_sessions": horizon,
            "observations": len(values),
            "independent_episodes": len({row["confirmed_episode_id"] for row, _ in observations}),
            "mean_forward_spy_return": statistics.fmean(values),
            "median_forward_spy_return": statistics.median(values),
            "negative_forward_return_rate": sum(value < 0 for value in values) / len(values),
            "diagnostic_only": True,
        })

    by_episode = defaultdict(list)
    for row in session_rows:
        if row["confirmed_episode_id"]:
            by_episode[row["confirmed_episode_id"]].append(row)
    segments = []
    for episode_id, rows in sorted(by_episode.items()):
        for segment, selected in (
            ("ENTRY", rows[:1]),
            ("EARLY", rows[1:3]),
            ("PERSISTENCE", rows[3:]),
        ):
            if not selected:
                continue
            segment_returns = []
            for source in selected:
                position = dates.index(source["date"])
                if returns[position] is not None:
                    segment_returns.append(returns[position])
            segments.append({
                "target_trend_state": target_trend_state,
                "episode_id": episode_id,
                "episode_start_date": rows[0]["date"],
                "episode_end_date": rows[-1]["date"],
                "segment": segment,
                "sessions": len(selected),
                "spy_return": _compound(segment_returns) if segment_returns else None,
                "diagnostic_only": True,
            })
    return session_rows, summaries, segments
