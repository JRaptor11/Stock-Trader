"""Locked Phase 011 calendar-block validation over immutable Phase 010 results."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
import zipfile
from collections import defaultdict
from pathlib import Path


ERAS = (
    ("OLDER_2008_2010", "prehistory", "2008-05-30", "2010-12-31", False),
    ("MODERN_2012_2014", "modern", "2012-01-01", "2014-12-31", False),
    ("MODERN_2015_2017", "modern", "2015-01-01", "2017-12-31", False),
    ("MODERN_2018_2020", "modern", "2018-01-01", "2020-12-31", False),
    ("MODERN_2021_2023", "modern", "2021-01-01", "2023-12-31", False),
    ("MODERN_2024_CURRENT", "modern", "2024-01-01", "9999-12-31", True),
)
COSTS = (10.0, 20.0)
BENCHMARK = "SPY_BUY_HOLD"


def _rows(archive: Path, name: str) -> list[dict]:
    with zipfile.ZipFile(archive) as bundle:
        return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8-sig"))))


def _daily_returns(rows: list[dict]) -> dict[tuple[str, float], list[dict]]:
    grouped: dict[tuple[str, float], list[dict]] = defaultdict(list)
    for row in rows:
        cost = float(row["cost_bps"])
        if cost in COSTS:
            grouped[(row["strategy"], cost)].append(row)
    result = {}
    for key, values in grouped.items():
        values.sort(key=lambda row: row["date"])
        previous = 100000.0
        output = []
        for row in values:
            equity = float(row["equity"])
            output.append({"date": row["date"], "return": equity / previous - 1.0})
            previous = equity
        result[key] = output
    return result


def _metrics(returns: list[float]) -> dict:
    if not returns:
        return {"sessions": 0, "total_return": None, "cagr": None,
                "annualized_volatility": None, "sharpe": None, "max_drawdown": None}
    wealth = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        max_drawdown = min(max_drawdown, wealth / peak - 1.0)
    volatility = statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    mean = statistics.fmean(returns) * 252
    return {
        "sessions": len(returns), "total_return": wealth - 1.0,
        "cagr": wealth ** (252 / len(returns)) - 1.0,
        "annualized_volatility": volatility,
        "sharpe": mean / volatility if volatility else 0.0,
        "max_drawdown": max_drawdown,
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def _episode_groups(rows: list[dict]) -> list[list[dict]]:
    episodes, current = [], []
    for row in rows:
        if current and row["date"] != current[-1]["next_date"]:
            episodes.append(current); current = []
        current.append(row)
    if current: episodes.append(current)
    return episodes


def build_validation(prehistory: Path, modern: Path, output: Path) -> dict:
    sources = {"prehistory": prehistory, "modern": modern}
    paths = {}; labels = {}
    for source, archive in sources.items():
        paths[source] = _daily_returns(_rows(archive, "tier1_daily.csv"))
        labels[source] = {
            row["date"]: row for row in _rows(archive, "tier1_market_state_labels.csv")
        }
    era_rows = []
    state_rows = []
    for era, source, start, end, partial in ERAS:
        source_paths = paths[source]
        dates = sorted({row["date"] for values in source_paths.values()
                        for row in values if start <= row["date"] <= end})
        next_dates = {day: dates[index + 1] if index + 1 < len(dates) else ""
                      for index, day in enumerate(dates)}
        benchmark_metrics = {}
        for cost in COSTS:
            spy = [row["return"] for row in source_paths[(BENCHMARK, cost)]
                   if start <= row["date"] <= end]
            benchmark_metrics[cost] = _metrics(spy)
        for (strategy, cost), values in sorted(source_paths.items()):
            selected = [row for row in values if start <= row["date"] <= end]
            metrics = _metrics([row["return"] for row in selected])
            benchmark = benchmark_metrics[cost]
            if not metrics["sessions"]: continue
            relative = ((1 + metrics["total_return"]) /
                        (1 + benchmark["total_return"]) - 1)
            era_rows.append({
                "era": era, "source": source, "start": start,
                "end": selected[-1]["date"], "partial": partial,
                "strategy": strategy, "cost_bps": cost, **metrics,
                "spy_total_return": benchmark["total_return"],
                "relative_wealth_vs_spy": relative,
                "beat_spy": relative > 0,
                "drawdown_better_than_spy": metrics["max_drawdown"] > benchmark["max_drawdown"],
                "sharpe_better_than_spy": metrics["sharpe"] > benchmark["sharpe"],
                "spy_cagr_retention": (metrics["cagr"] / benchmark["cagr"]
                                       if benchmark["cagr"] and benchmark["cagr"] > 0 else None),
            })
            if cost != 10.0: continue
            joined = []
            for row in selected:
                state = labels[source].get(row["date"])
                if state:
                    joined.append({**row, "trend_state": state["trend_state"],
                                   "next_date": next_dates.get(row["date"], "")})
            spy_by_date = {row["date"]: row["return"]
                           for row in source_paths[(BENCHMARK, cost)]}
            for trend_state in sorted({row["trend_state"] for row in joined}):
                state_values = [row for row in joined if row["trend_state"] == trend_state]
                strategy_return = _metrics([row["return"] for row in state_values])["total_return"]
                spy_return = _metrics([spy_by_date[row["date"]] for row in state_values])["total_return"]
                episodes = _episode_groups(state_values)
                episode_excess = []
                for episode in episodes:
                    strategy_episode = _metrics([row["return"] for row in episode])["total_return"]
                    spy_episode = _metrics([spy_by_date[row["date"]] for row in episode])["total_return"]
                    episode_excess.append((1 + strategy_episode) / (1 + spy_episode) - 1)
                state_rows.append({
                    "era": era, "strategy": strategy, "trend_state": trend_state,
                    "cost_bps": cost, "sessions": len(state_values),
                    "episodes": len(episodes),
                    "sample_sufficient": len(state_values) >= 50 and len(episodes) >= 5,
                    "strategy_compounded_return": strategy_return,
                    "spy_compounded_return": spy_return,
                    "relative_wealth_vs_spy": (1 + strategy_return) / (1 + spy_return) - 1,
                    "episode_beat_spy_rate": (sum(value > 0 for value in episode_excess) /
                                              len(episode_excess) if episode_excess else None),
                    "median_episode_excess": (statistics.median(episode_excess)
                                              if episode_excess else None),
                    "worst_episode_excess": min(episode_excess) if episode_excess else None,
                })
    _write(output / "phase011_era_scorecard.csv", era_rows)
    _write(output / "phase011_state_episode_scorecard.csv", state_rows)

    strategy_rows = []
    strategies = sorted({row["strategy"] for row in era_rows if row["strategy"] != BENCHMARK})
    for strategy in strategies:
        by_cost = {cost: [row for row in era_rows
                          if row["strategy"] == strategy and row["cost_bps"] == cost]
                   for cost in COSTS}
        primary = by_cost[10.0]
        relative = [row["relative_wealth_vs_spy"] for row in primary]
        retention = [row["spy_cagr_retention"] for row in primary
                     if row["spy_cagr_retention"] is not None]
        return_pass = all(
            sum(row["beat_spy"] for row in by_cost[cost]) >= 4 and
            statistics.median(row["relative_wealth_vs_spy"] for row in by_cost[cost]) > 0 and
            min(row["relative_wealth_vs_spy"] for row in by_cost[cost]) >= -0.10
            for cost in COSTS
        )
        stability_pass = (
            sum(row["drawdown_better_than_spy"] for row in primary) >= 5 and
            sum(row["sharpe_better_than_spy"] for row in primary) >= 4 and
            bool(retention) and statistics.median(retention) >= 0.75
        )
        strategy_rows.append({
            "strategy": strategy, "blocks": len(primary),
            "blocks_beating_spy_10bps": sum(row["beat_spy"] for row in primary),
            "median_relative_wealth_10bps": statistics.median(relative),
            "worst_relative_wealth_10bps": min(relative),
            "blocks_lower_drawdown_10bps": sum(row["drawdown_better_than_spy"] for row in primary),
            "blocks_higher_sharpe_10bps": sum(row["sharpe_better_than_spy"] for row in primary),
            "median_spy_cagr_retention": statistics.median(retention) if retention else None,
            "return_contender_pass": return_pass,
            "stability_baseline_pass": stability_pass,
        })
    _write(output / "phase011_strategy_acceptance.csv", strategy_rows)

    specialist_rows = []
    for strategy in strategies:
        states = sorted({row["trend_state"] for row in state_rows
                         if row["strategy"] == strategy})
        for state in states:
            eligible = [row for row in state_rows if row["strategy"] == strategy and
                        row["trend_state"] == state and row["sample_sufficient"]]
            if not eligible: continue
            episodes = sum(row["episodes"] for row in eligible)
            weighted_wins = sum(row["episode_beat_spy_rate"] * row["episodes"]
                                for row in eligible)
            specialist_rows.append({
                "strategy": strategy, "trend_state": state,
                "eligible_blocks": len(eligible),
                "positive_relative_blocks": sum(row["relative_wealth_vs_spy"] > 0
                                                for row in eligible),
                "aggregate_episodes": episodes,
                "aggregate_episode_win_rate": weighted_wins / episodes if episodes else None,
                "median_block_relative_wealth": statistics.median(
                    row["relative_wealth_vs_spy"] for row in eligible),
                "specialist_gate_pass": (
                    len(eligible) >= 3 and
                    sum(row["relative_wealth_vs_spy"] > 0 for row in eligible) >= 2 and
                    episodes > 0 and weighted_wins / episodes >= 0.60
                ),
            })
    _write(output / "phase011_state_specialist_acceptance.csv", specialist_rows)
    summary = {
        "study_id": "LONG_TERM_MULTI_ERA_VALIDATION_PHASE_011",
        "eras": len(ERAS), "strategies": len(strategies),
        "return_contenders_passed": [row["strategy"] for row in strategy_rows
                                     if row["return_contender_pass"]],
        "stability_baselines_passed": [row["strategy"] for row in strategy_rows
                                      if row["stability_baseline_pass"]],
        "state_specialists_passed": [
            {"strategy": row["strategy"], "trend_state": row["trend_state"]}
            for row in specialist_rows if row["specialist_gate_pass"]
        ],
        "router_effect": "none", "paper_trading_effect": "none",
    }
    (output / "phase011_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prehistory", type=Path, required=True)
    parser.add_argument("--modern", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_validation(args.prehistory, args.modern, args.output), indent=2))


if __name__ == "__main__":
    main()
