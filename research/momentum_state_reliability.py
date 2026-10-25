"""Frozen episode/payoff reliability audit for existing state specialists."""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import zipfile
from collections import defaultdict
from pathlib import Path


BENCHMARK = "SPY_BUY_HOLD"
COSTS = (10.0, 20.0)
LAGS = (0, 1, 3)
BLOCKS = (
    ("OLDER_2008_2010", "prehistory", "2008-05-30", "2010-12-31"),
    ("MODERN_2012_2014", "modern", "2012-01-01", "2014-12-31"),
    ("MODERN_2015_2017", "modern", "2015-01-01", "2017-12-31"),
    ("MODERN_2018_2020", "modern", "2018-01-01", "2020-12-31"),
    ("MODERN_2021_2023", "modern", "2021-01-01", "2023-12-31"),
    ("MODERN_2024_CURRENT", "modern", "2024-01-01", "9999-12-31"),
)
HYPOTHESES = (
    ("REL_MOM_BULL_DECELERATING", "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", "trend_state", "BULL_DECELERATING"),
    ("TREND_BEAR_RECOVERING", "DIVERSIFIED_TREND", "trend_state", "BEAR_RECOVERING"),
    ("REL_MOM_BULL_ACCEL_HIGH_VOL_BROAD", "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", "core_state", "BULL_ACCELERATING__HIGH_VOL__BROAD_BREADTH"),
)


def _read(archive: Path, name: str) -> list[dict]:
    with zipfile.ZipFile(archive) as bundle:
        return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8-sig"))))


def _paths(rows: list[dict], strategies: set[str]) -> dict[tuple[str, float], dict[str, float]]:
    grouped = defaultdict(list)
    for row in rows:
        key = (row["strategy"], float(row["cost_bps"]))
        if key[0] in strategies and key[1] in COSTS:
            grouped[key].append(row)
    result = {}
    for key, values in grouped.items():
        previous = 100000.0
        daily = {}
        for row in sorted(values, key=lambda value: value["date"]):
            equity = float(row["equity"])
            daily[row["date"]] = equity / previous - 1.0
            previous = equity
        result[key] = daily
    return result


def _episodes(labels: list[dict], dates: list[str], field: str, state: str) -> list[list[str]]:
    position = {day: index for index, day in enumerate(dates)}
    eligible = [row["date"] for row in labels if row.get(field) == state and row["date"] in position]
    result, current = [], []
    for day in eligible:
        if current and position[day] != position[current[-1]] + 1:
            result.append(current); current = []
        current.append(day)
    if current:
        result.append(current)
    return result


def _compound(path: dict[str, float], dates: list[str]) -> float:
    wealth = 1.0
    for day in dates:
        wealth *= 1.0 + path[day]
    return wealth - 1.0


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8"); return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def _metrics(rows: list[dict]) -> dict:
    excess = [float(row["relative_wealth_vs_spy"]) for row in rows]
    gains = [value for value in excess if value > 0]
    losses = [value for value in excess if value < 0]
    consecutive = maximum = 0
    for row in rows:
        consecutive = 0 if row["beat_spy"] else consecutive + 1
        maximum = max(maximum, consecutive)
    return {
        "episodes": len(rows),
        "episode_win_rate": sum(bool(row["beat_spy"]) for row in rows) / len(rows) if rows else None,
        "median_episode_excess": statistics.median(excess) if excess else None,
        "mean_episode_excess": statistics.fmean(excess) if excess else None,
        "average_win": statistics.fmean(gains) if gains else None,
        "average_loss": statistics.fmean(losses) if losses else None,
        "payoff_ratio": (statistics.fmean(gains) / abs(statistics.fmean(losses)) if gains and losses else None),
        "profit_factor": (sum(gains) / abs(sum(losses)) if gains and losses else None),
        "worst_episode": min(excess) if excess else None,
        "best_episode": max(excess) if excess else None,
        "maximum_consecutive_losses": maximum,
    }


def build(prehistory: Path, modern: Path, output: Path) -> dict:
    archives = {"prehistory": prehistory, "modern": modern}
    strategies = {BENCHMARK, *(item[1] for item in HYPOTHESES)}
    source_data = {}
    for source, archive in archives.items():
        source_data[source] = (
            _paths(_read(archive, "tier1_daily.csv"), strategies),
            _read(archive, "tier1_market_state_labels.csv"),
        )
    episode_rows = []
    for block, source, start, end in BLOCKS:
        paths, labels = source_data[source]
        all_dates = sorted(day for day in paths[(BENCHMARK, 10.0)] if start <= day <= end)
        next_day = {day: all_dates[index + 1] for index, day in enumerate(all_dates[:-1])}
        block_labels = [row for row in labels if start <= row["date"] <= end]
        for hypothesis, strategy, field, state in HYPOTHESES:
            for number, label_days in enumerate(_episodes(block_labels, all_dates, field, state), 1):
                causal_days = [next_day[day] for day in label_days if day in next_day]
                for cost in COSTS:
                    for lag in LAGS:
                        selected = causal_days[lag:]
                        if not selected:
                            continue
                        strategy_return = _compound(paths[(strategy, cost)], selected)
                        spy_return = _compound(paths[(BENCHMARK, cost)], selected)
                        excess = (1 + strategy_return) / (1 + spy_return) - 1
                        episode_rows.append({
                            "hypothesis": hypothesis, "strategy": strategy, "state": state,
                            "block": block, "episode_id": f"{block}-{hypothesis}-{number:03d}",
                            "label_start": label_days[0], "label_end": label_days[-1],
                            "sessions": len(selected), "cost_bps": cost,
                            "activation_lag_sessions": lag, "strategy_return": strategy_return,
                            "spy_return": spy_return, "relative_wealth_vs_spy": excess,
                            "beat_spy": excess > 0,
                        })
    _write(output / "reliability_episode_scorecard.csv", episode_rows)
    aggregate = []
    for hypothesis, strategy, _, state in HYPOTHESES:
        for cost in COSTS:
            for lag in LAGS:
                rows = [row for row in episode_rows if row["hypothesis"] == hypothesis and
                        row["cost_bps"] == cost and row["activation_lag_sessions"] == lag]
                values = _metrics(rows)
                blocks = sorted({row["block"] for row in rows})
                positive_blocks = sum(
                    statistics.fmean([float(row["relative_wealth_vs_spy"]) for row in rows if row["block"] == block]) > 0
                    for block in blocks
                )
                aggregate.append({"hypothesis": hypothesis, "strategy": strategy, "state": state,
                                  "cost_bps": cost, "activation_lag_sessions": lag,
                                  "eligible_blocks": len(blocks), "positive_blocks": positive_blocks,
                                  **values})
    _write(output / "reliability_aggregate_scorecard.csv", aggregate)
    verdicts = []
    for hypothesis, strategy, _, state in HYPOTHESES:
        primary = next(row for row in aggregate if row["hypothesis"] == hypothesis and row["cost_bps"] == 10.0 and row["activation_lag_sessions"] == 0)
        stress = next(row for row in aggregate if row["hypothesis"] == hypothesis and row["cost_bps"] == 20.0 and row["activation_lag_sessions"] == 1)
        passed = (primary["episodes"] >= 20 and primary["eligible_blocks"] >= 3 and
                  primary["positive_blocks"] >= 2 and primary["episode_win_rate"] >= .60 and
                  primary["median_episode_excess"] > 0 and (primary["profit_factor"] or 0) >= 1.25 and
                  stress["episode_win_rate"] >= .55 and stress["median_episode_excess"] > 0)
        verdicts.append({"hypothesis": hypothesis, "strategy": strategy, "state": state,
                         "primary_episodes": primary["episodes"], "primary_win_rate": primary["episode_win_rate"],
                         "primary_median_excess": primary["median_episode_excess"],
                         "primary_profit_factor": primary["profit_factor"],
                         "primary_payoff_ratio": primary["payoff_ratio"],
                         "positive_blocks": primary["positive_blocks"], "eligible_blocks": primary["eligible_blocks"],
                         "stress_win_rate": stress["episode_win_rate"], "stress_median_excess": stress["median_episode_excess"],
                         "reliability_gate_pass": passed})
    _write(output / "reliability_acceptance.csv", verdicts)
    summary = {"study_id": "MOMENTUM_STATE_RELIABILITY_STUDY_001",
               "hypotheses": len(HYPOTHESES),
               "passed": [row["hypothesis"] for row in verdicts if row["reliability_gate_pass"]],
               "router_effect": "none", "paper_trading_effect": "none", "generation_015_effect": "none"}
    output.mkdir(parents=True, exist_ok=True)
    (output / "reliability_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prehistory", type=Path, required=True)
    parser.add_argument("--modern", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.prehistory, args.modern, args.output), indent=2))


if __name__ == "__main__":
    main()
