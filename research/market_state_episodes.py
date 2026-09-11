"""Causal, episode-based market-state attribution for strategy research."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict


STATE_DEFINITION = {
    "schema_version": 1,
    "causality": "The label for session t uses only market data available through session t-1.",
    "core_state": "trend_state + volatility_state + breadth_state",
    "trend_state": {
        "BULL_ACCELERATING": "Above the 200-day mean, positive 63-day return, and nonnegative 20-day return and 5-day trend acceleration.",
        "BULL_DECELERATING": "Above the 200-day mean with positive 63-day return, but short trend or acceleration is negative.",
        "BEAR_DETERIORATING": "Below the 200-day mean, negative 63-day return, and negative 20-day return and 5-day trend acceleration.",
        "BEAR_RECOVERING": "Below the 200-day mean with negative 63-day return, but short trend or acceleration is no longer jointly negative.",
        "TRANSITION_IMPROVING": "Long- and medium-term directions disagree while short trend and acceleration are nonnegative.",
        "TRANSITION_DETERIORATING": "Long- and medium-term directions disagree while short trend and acceleration are negative.",
        "TRANSITION_MIXED": "Long- and medium-term directions disagree without aligned short-term direction and acceleration.",
    },
    "volatility_state": {
        "LOW": "Causal expanding-history percentile is in the lowest two quintiles.",
        "NORMAL": "Causal expanding-history percentile is in the middle quintile.",
        "HIGH": "Causal expanding-history percentile is in the highest two quintiles.",
    },
    "breadth_state": {
        "NARROW": "At most 35% of non-benchmark assets are above their 50-day mean.",
        "MIXED": "More than 35% but less than 65% are above their 50-day mean.",
        "BROAD": "At least 65% are above their 50-day mean.",
    },
    "transition_overlays": {
        "trend_transition": "Sign of the causal 5-session change in the 20-day market return.",
        "volatility_transition": "Sign of the causal 5-session change in 20-day annualized volatility.",
        "breadth_transition": "Sign of the causal 5-session change in 50-day breadth.",
        "correlation_state": "Causal expanding-history quintile of average 20-day asset/market correlation.",
        "dispersion_state": "Causal expanding-history quintile of cross-sectional 20-day return dispersion.",
    },
    "episode_rule": "A new episode starts after a proposed core state persists for three consecutive sessions. Raw labels remain recorded. Confirmation uses no future observations and prior sessions are not relabeled.",
    "state_change_confirmation_sessions": 3,
    "minimum_evidence": {"pooled_sessions": 30, "distinct_episodes": 5},
    "interpretation": "State results are descriptive research evidence and never authorize strategy switching or live allocation.",
}


def _compound(values: list[float]) -> float:
    return math.prod(1.0 + value for value in values) - 1.0


def _maximum_drawdown(values: list[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def _trend_state(state: dict) -> str | None:
    long_term = state.get("trend_200d_distance")
    medium = state.get("trend_63d_return")
    short = state.get("trend_20d_return")
    acceleration = state.get("trend_acceleration_5d")
    if None in (long_term, medium, short, acceleration):
        return None
    if long_term >= 0 and medium >= 0:
        return "BULL_ACCELERATING" if short >= 0 and acceleration >= 0 else "BULL_DECELERATING"
    if long_term < 0 and medium < 0:
        return "BEAR_DETERIORATING" if short < 0 and acceleration < 0 else "BEAR_RECOVERING"
    if short >= 0 and acceleration >= 0:
        return "TRANSITION_IMPROVING"
    if short < 0 and acceleration < 0:
        return "TRANSITION_DETERIORATING"
    return "TRANSITION_MIXED"


def _volatility_state(state: dict) -> str | None:
    bucket = state.get("volatility_20d_bucket")
    if not bucket:
        return None
    if bucket in ("Q1_LOW", "Q2"):
        return "LOW"
    if bucket in ("Q4", "Q5_HIGH"):
        return "HIGH"
    return "NORMAL"


def _breadth_state(state: dict) -> str | None:
    breadth = state.get("breadth_50d")
    if breadth is None:
        return None
    if breadth >= 0.65:
        return "BROAD"
    if breadth <= 0.35:
        return "NARROW"
    return "MIXED"


def _direction(value, deadband: float = 0.0) -> str | None:
    if value is None:
        return None
    if value > deadband:
        return "RISING"
    if value < -deadband:
        return "FALLING"
    return "STABLE"


def causal_state_labels(
    conditions: dict[str, dict], confirmation_sessions: int = 3,
) -> dict[str, dict]:
    """Collapse continuous causal features into interpretable state axes."""
    if confirmation_sessions < 1:
        raise ValueError("confirmation_sessions must be positive")
    raw_rows = []
    for day, source in sorted(conditions.items()):
        trend = _trend_state(source)
        volatility = _volatility_state(source)
        breadth = _breadth_state(source)
        if not all((trend, volatility, breadth)):
            continue
        raw_rows.append({
            "date": day,
            "raw_trend_state": trend,
            "raw_volatility_state": volatility,
            "raw_breadth_state": breadth,
            "raw_core_state": f"{trend}__{volatility}_VOL__{breadth}_BREADTH",
            "trend_transition": _direction(source.get("trend_acceleration_5d")),
            "volatility_transition": _direction(source.get("volatility_change_5d")),
            "breadth_transition": _direction(source.get("breadth_50d_change_5d")),
            "correlation_state": source.get("correlation_20d_bucket"),
            "dispersion_state": source.get("dispersion_20d_bucket"),
        })
    result = {}
    active = pending = None
    pending_count = 0
    active_components = None
    for row in raw_rows:
        proposed = row["raw_core_state"]
        proposed_components = (
            row["raw_trend_state"], row["raw_volatility_state"],
            row["raw_breadth_state"],
        )
        changed = False
        if active is None:
            active, active_components = proposed, proposed_components
        elif proposed == active:
            pending, pending_count = None, 0
        else:
            if proposed == pending:
                pending_count += 1
            else:
                pending, pending_count = proposed, 1
            if pending_count >= confirmation_sessions:
                active, active_components = proposed, proposed_components
                pending, pending_count, changed = None, 0, True
        result[row["date"]] = {
            **row, "trend_state": active_components[0],
            "volatility_state": active_components[1],
            "breadth_state": active_components[2], "core_state": active,
            "state_changed": changed, "pending_core_state": pending,
            "pending_confirmation_sessions": pending_count,
        }
    return result


def state_episodes(labels: dict[str, dict]) -> tuple[list[dict], dict[str, str]]:
    """Assign contiguous observations of the same core state to causal episodes."""
    episodes = []
    date_to_episode = {}
    active = None
    for day, label in sorted(labels.items()):
        state = label["core_state"]
        if active is None or active["core_state"] != state:
            if active:
                episodes.append(active)
            active = {
                "episode_id": f"STATE:{len(episodes) + 1:05d}",
                "core_state": state,
                "start_date": day,
                "end_date": day,
                "sessions": 0,
            }
        active["end_date"] = day
        active["sessions"] += 1
        date_to_episode[day] = active["episode_id"]
    if active:
        episodes.append(active)
    return episodes, date_to_episode


def market_state_scorecards(
    daily: list[dict], conditions: dict[str, dict], primary_cost_bps: float,
    benchmark_strategy: str = "SPY_BUY_HOLD", minimum_state_sessions: int = 30,
    minimum_state_episodes: int = 5,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Attribute every strategy to pooled state sessions and repeated episodes."""
    labels = causal_state_labels(conditions)
    episodes, episode_map = state_episodes(labels)
    selected = sorted(
        (row for row in daily if float(row["cost_bps"]) == float(primary_cost_bps)),
        key=lambda row: (row["strategy"], row["date"]),
    )
    observations = []
    prior_equity = {}
    for row in selected:
        strategy = row["strategy"]
        equity = float(row["equity"])
        prior = prior_equity.get(strategy)
        prior_equity[strategy] = equity
        if prior and row["date"] in labels:
            observations.append({
                "strategy": strategy, "date": row["date"],
                "return": equity / prior - 1.0,
                "episode_id": episode_map[row["date"]], **labels[row["date"]],
            })
    benchmark = {
        row["date"]: row["return"] for row in observations
        if row["strategy"] == benchmark_strategy
    }
    episode_groups = defaultdict(list)
    for row in observations:
        episode_groups[(row["strategy"], row["core_state"], row["episode_id"])].append(row)
    episode_rows = []
    for (strategy, state, episode_id), rows in sorted(episode_groups.items()):
        returns = [row["return"] for row in rows]
        benchmark_returns = [benchmark[row["date"]] for row in rows if row["date"] in benchmark]
        if len(benchmark_returns) != len(returns):
            continue
        strategy_return, spy_return = _compound(returns), _compound(benchmark_returns)
        episode_rows.append({
            "strategy": strategy, "core_state": state, "episode_id": episode_id,
            "start_date": rows[0]["date"], "end_date": rows[-1]["date"],
            "sessions": len(rows), "strategy_return": strategy_return,
            "spy_return": spy_return,
            "excess_return_vs_spy": (1.0 + strategy_return) / (1.0 + spy_return) - 1.0,
            "maximum_drawdown": _maximum_drawdown(returns),
        })
    state_groups = defaultdict(list)
    for row in observations:
        state_groups[(row["strategy"], row["core_state"])].append(row)
    episode_lookup = defaultdict(list)
    for row in episode_rows:
        episode_lookup[(row["strategy"], row["core_state"])].append(row)
    total_sessions = len({row["date"] for row in observations})
    state_rows = []
    for (strategy, state), rows in sorted(state_groups.items()):
        returns = [row["return"] for row in rows]
        benchmark_returns = [benchmark[row["date"]] for row in rows if row["date"] in benchmark]
        if len(benchmark_returns) != len(returns):
            continue
        strategy_return, spy_return = _compound(returns), _compound(benchmark_returns)
        occurrences = episode_lookup[(strategy, state)]
        occurrence_returns = [row["strategy_return"] for row in occurrences]
        state_rows.append({
            "strategy": strategy, "core_state": state,
            "trend_state": rows[0]["trend_state"],
            "volatility_state": rows[0]["volatility_state"],
            "breadth_state": rows[0]["breadth_state"],
            "cost_bps": primary_cost_bps, "sessions": len(rows),
            "occupancy_pct": len(rows) / total_sessions if total_sessions else None,
            "episodes": len(occurrences),
            "sample_sufficient": (
                len(rows) >= minimum_state_sessions
                and len(occurrences) >= minimum_state_episodes
            ),
            "strategy_compounded_return": strategy_return,
            "spy_compounded_return": spy_return,
            "relative_wealth_vs_spy": (1.0 + strategy_return) / (1.0 + spy_return) - 1.0,
            "mean_daily_return": statistics.fmean(returns),
            "median_daily_return": statistics.median(returns),
            "daily_win_rate": sum(value > 0 for value in returns) / len(returns),
            "conditional_sequence_max_drawdown": _maximum_drawdown(returns),
            "mean_episode_sessions": statistics.fmean(row["sessions"] for row in occurrences),
            "median_episode_sessions": statistics.median(row["sessions"] for row in occurrences),
            "mean_episode_return": statistics.fmean(occurrence_returns),
            "median_episode_return": statistics.median(occurrence_returns),
            "positive_episode_rate": sum(value > 0 for value in occurrence_returns) / len(occurrence_returns),
            "episode_beat_spy_rate": sum(row["excess_return_vs_spy"] > 0 for row in occurrences) / len(occurrences),
            "worst_episode_return": min(occurrence_returns),
            "best_episode_return": max(occurrence_returns),
            "log_return_contribution": sum(math.log1p(value) for value in returns),
        })
    return list(labels.values()), episodes, state_rows, episode_rows
