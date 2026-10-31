"""Causal slow/fast momentum-cycle diagnostics for frozen research archives.

The four states follow Goulding, Harvey, and Mazzoleni's sign convention:
positive slow/positive fast is Bull, positive slow/negative fast is Correction,
negative slow/negative fast is Bear, and negative slow/positive fast is
Rebound.  A session is labelled only from closes available before that
session, matching the replay engine's signal-at-close/next-open semantics.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict


TURNING_POINT_CYCLE_DEFINITION = {
    "version": 1,
    "slow_lookback_sessions": 252,
    "fast_lookback_sessions": 21,
    "signal_timing": "prior completed close",
    "execution_alignment": "label applies to the following session",
    "states": {
        "BULL": {"slow": "nonnegative", "fast": "nonnegative"},
        "CORRECTION": {"slow": "nonnegative", "fast": "negative"},
        "BEAR": {"slow": "negative", "fast": "negative"},
        "REBOUND": {"slow": "negative", "fast": "nonnegative"},
    },
}


def _return(closes: list[float], sessions: int) -> float | None:
    if len(closes) <= sessions or not closes[-sessions - 1]:
        return None
    return closes[-1] / closes[-sessions - 1] - 1.0


def _cycle(slow: float, fast: float) -> str:
    if slow >= 0.0:
        return "BULL" if fast >= 0.0 else "CORRECTION"
    return "REBOUND" if fast >= 0.0 else "BEAR"


def causal_turning_point_labels(
    dates: list[str],
    bars: dict,
    benchmark_symbol: str,
    scored_start: str,
    slow_lookback: int = 252,
    fast_lookback: int = 21,
) -> list[dict]:
    """Label scored sessions from information available at the prior close."""
    closes: list[float] = []
    rows: list[dict] = []
    for day in dates:
        slow = _return(closes, slow_lookback)
        fast = _return(closes, fast_lookback)
        if day >= scored_start and slow is not None and fast is not None:
            rows.append({
                "date": day,
                "turning_point_cycle": _cycle(slow, fast),
                "slow_return": slow,
                "fast_return": fast,
                "slow_lookback_sessions": slow_lookback,
                "fast_lookback_sessions": fast_lookback,
                "signal_as_of": "prior_close",
            })
        closes.append(float(bars[day][benchmark_symbol]["close"]))
    return rows


def _daily_returns(daily: list[dict], initial_cash: float) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in daily:
        grouped[row["strategy"]].append(row)
    paths: dict[str, dict[str, float]] = {}
    for strategy, rows in grouped.items():
        previous = initial_cash
        path: dict[str, float] = {}
        for row in sorted(rows, key=lambda item: item["date"]):
            equity = float(row["equity"])
            path[row["date"]] = equity / previous - 1.0
            previous = equity
        paths[strategy] = path
    return paths


def _compound(path: dict[str, float], dates: list[str]) -> float:
    wealth = 1.0
    for day in dates:
        wealth *= 1.0 + path[day]
    return wealth - 1.0


def _episodes(labels: list[dict]) -> list[dict]:
    if not labels:
        return []
    episodes: list[dict] = []
    current_state = labels[0]["turning_point_cycle"]
    current_dates: list[str] = []
    episode_number = 0
    for row in labels:
        state = row["turning_point_cycle"]
        if state != current_state:
            episode_number += 1
            episodes.append({
                "episode_number": episode_number,
                "turning_point_cycle": current_state,
                "episode_start": current_dates[0],
                "episode_end": current_dates[-1],
                "sessions": len(current_dates),
                "complete_episode": episode_number > 1,
                "dates": tuple(current_dates),
            })
            current_state = state
            current_dates = []
        current_dates.append(row["date"])
    episode_number += 1
    episodes.append({
        "episode_number": episode_number,
        "turning_point_cycle": current_state,
        "episode_start": current_dates[0],
        "episode_end": current_dates[-1],
        "sessions": len(current_dates),
        "complete_episode": False,
        "dates": tuple(current_dates),
    })
    if len(episodes) == 1:
        episodes[0]["complete_episode"] = False
    return episodes


def turning_point_cycle_scorecards(
    daily: list[dict],
    labels: list[dict],
    initial_cash: float,
    benchmark_strategy: str = "SPY_BUY_HOLD",
    cost_bps: float | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Return episode definitions, state attribution, and episode returns."""
    paths = _daily_returns(daily, initial_cash)
    benchmark = paths.get(benchmark_strategy, {})
    label_dates: dict[str, list[str]] = defaultdict(list)
    for row in labels:
        label_dates[row["turning_point_cycle"]].append(row["date"])

    attribution: list[dict] = []
    for strategy, path in sorted(paths.items()):
        for state in TURNING_POINT_CYCLE_DEFINITION["states"]:
            dates = [day for day in label_dates[state] if day in path and day in benchmark]
            if not dates:
                continue
            strategy_return = _compound(path, dates)
            benchmark_return = _compound(benchmark, dates)
            relative = (1.0 + strategy_return) / (1.0 + benchmark_return) - 1.0
            excess = [path[day] - benchmark[day] for day in dates]
            attribution.append({
                "strategy": strategy,
                "benchmark": benchmark_strategy,
                "cost_bps": cost_bps,
                "turning_point_cycle": state,
                "sessions": len(dates),
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "relative_wealth_vs_benchmark": relative,
                "mean_daily_excess": statistics.fmean(excess),
                "daily_win_rate": sum(value > 0.0 for value in excess) / len(excess),
            })

    raw_episodes = _episodes(labels)
    episode_definitions = [
        {key: value for key, value in episode.items() if key != "dates"}
        for episode in raw_episodes
    ]
    episode_returns: list[dict] = []
    for episode in raw_episodes:
        dates = list(episode["dates"])
        for strategy, path in sorted(paths.items()):
            selected = [day for day in dates if day in path and day in benchmark]
            if len(selected) != len(dates):
                continue
            strategy_return = _compound(path, selected)
            benchmark_return = _compound(benchmark, selected)
            episode_returns.append({
                "strategy": strategy,
                "benchmark": benchmark_strategy,
                "cost_bps": cost_bps,
                **{key: value for key, value in episode.items() if key != "dates"},
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "relative_wealth_vs_benchmark": (
                    (1.0 + strategy_return) / (1.0 + benchmark_return) - 1.0
                ),
            })
    return episode_definitions, attribution, episode_returns
