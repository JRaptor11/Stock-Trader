"""Frozen Phase 012 validation of bear-recovering specialist hypotheses."""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import zipfile
from collections import defaultdict
from pathlib import Path


STATE = "BEAR_RECOVERING"
BENCHMARK = "SPY_BUY_HOLD"
CANDIDATES = (
    "CROSS_ASSET_DUAL_MOMENTUM",
    "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE",
    "DIVERSIFIED_TREND",
)
COSTS = (10.0, 20.0)
ACTIVATION_LAGS = (0, 1, 3)
SOURCES = (
    ("prehistory", "2008-03-14", "2010-12-31"),
    ("modern", "2012-01-01", "9999-12-31"),
)


def _rows(archive: Path, name: str) -> list[dict]:
    with zipfile.ZipFile(archive) as bundle:
        return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8-sig"))))


def _daily_returns(rows: list[dict]) -> dict[tuple[str, float], dict[str, float]]:
    grouped: dict[tuple[str, float], list[dict]] = defaultdict(list)
    for row in rows:
        cost = float(row["cost_bps"])
        if cost in COSTS and row["strategy"] in (BENCHMARK, *CANDIDATES):
            grouped[(row["strategy"], cost)].append(row)
    result = {}
    for key, values in grouped.items():
        values.sort(key=lambda item: item["date"])
        previous = 100000.0
        path = {}
        for row in values:
            equity = float(row["equity"])
            path[row["date"]] = equity / previous - 1.0
            previous = equity
        result[key] = path
    return result


def _episodes(labels: list[dict], trading_dates: list[str]) -> list[list[str]]:
    position = {day: index for index, day in enumerate(trading_dates)}
    eligible = [row["date"] for row in labels if row["trend_state"] == STATE and
                row["date"] in position]
    episodes: list[list[str]] = []
    current: list[str] = []
    for day in eligible:
        if current and position[day] != position[current[-1]] + 1:
            episodes.append(current)
            current = []
        current.append(day)
    if current:
        episodes.append(current)
    return episodes


def _compound(values: list[float]) -> float:
    wealth = 1.0
    for value in values:
        wealth *= 1.0 + value
    return wealth - 1.0


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build_validation(prehistory: Path, modern: Path, output: Path) -> dict:
    archives = {"prehistory": prehistory, "modern": modern}
    episode_rows = []
    for source, start, end in SOURCES:
        paths = _daily_returns(_rows(archives[source], "tier1_daily.csv"))
        labels = _rows(archives[source], "tier1_market_state_labels.csv")
        dates = sorted(day for day in paths[(BENCHMARK, 10.0)] if start <= day <= end)
        next_day = {day: dates[index + 1] for index, day in enumerate(dates[:-1])}
        filtered_labels = [row for row in labels if start <= row["date"] <= end]
        for episode_number, label_days in enumerate(_episodes(filtered_labels, dates), 1):
            # A label observed after a completed session can only activate on its next session.
            return_days = [next_day[day] for day in label_days if day in next_day]
            for cost in COSTS:
                spy_path = paths[(BENCHMARK, cost)]
                for strategy in CANDIDATES:
                    strategy_path = paths[(strategy, cost)]
                    for lag in ACTIVATION_LAGS:
                        selected = return_days[lag:]
                        if not selected:
                            continue
                        strategy_return = _compound([strategy_path[day] for day in selected])
                        spy_return = _compound([spy_path[day] for day in selected])
                        relative = (1.0 + strategy_return) / (1.0 + spy_return) - 1.0
                        episode_rows.append({
                            "source": source,
                            "episode_id": f"{source}-{episode_number:03d}",
                            "label_start": label_days[0],
                            "label_end": label_days[-1],
                            "return_start": selected[0],
                            "return_end": selected[-1],
                            "sessions": len(selected),
                            "strategy": strategy,
                            "cost_bps": cost,
                            "activation_lag_sessions": lag,
                            "strategy_return": strategy_return,
                            "spy_return": spy_return,
                            "relative_wealth_vs_spy": relative,
                            "beat_spy": relative > 0,
                        })
    _write(output / "phase012_episode_scorecard.csv", episode_rows)

    aggregate_rows = []
    for strategy in CANDIDATES:
        for cost in COSTS:
            for lag in ACTIVATION_LAGS:
                selected = [row for row in episode_rows if row["strategy"] == strategy and
                            row["cost_bps"] == cost and
                            row["activation_lag_sessions"] == lag]
                relative = [row["relative_wealth_vs_spy"] for row in selected]
                source_rates = {}
                for source, _, _ in SOURCES:
                    source_rows = [row for row in selected if row["source"] == source]
                    source_rates[source] = (sum(row["beat_spy"] for row in source_rows) /
                                            len(source_rows) if source_rows else None)
                aggregate_rows.append({
                    "strategy": strategy,
                    "cost_bps": cost,
                    "activation_lag_sessions": lag,
                    "episodes": len(selected),
                    "episode_win_rate": (sum(row["beat_spy"] for row in selected) /
                                         len(selected) if selected else None),
                    "median_episode_excess": (statistics.median(relative)
                                               if relative else None),
                    "mean_episode_excess": (statistics.fmean(relative)
                                             if relative else None),
                    "worst_episode_excess": min(relative) if relative else None,
                    "prehistory_win_rate": source_rates["prehistory"],
                    "modern_win_rate": source_rates["modern"],
                })
    _write(output / "phase012_aggregate_scorecard.csv", aggregate_rows)

    verdicts = []
    for strategy in CANDIDATES:
        primary = next(row for row in aggregate_rows if row["strategy"] == strategy and
                       row["cost_bps"] == 10.0 and
                       row["activation_lag_sessions"] == 0)
        delayed = next(row for row in aggregate_rows if row["strategy"] == strategy and
                       row["cost_bps"] == 20.0 and
                       row["activation_lag_sessions"] == 1)
        passed = (
            primary["episodes"] >= 12 and
            primary["episode_win_rate"] >= 0.60 and
            primary["median_episode_excess"] > 0 and
            primary["prehistory_win_rate"] >= 0.50 and
            primary["modern_win_rate"] >= 0.50 and
            delayed["episode_win_rate"] >= 0.55 and
            delayed["median_episode_excess"] > 0
        )
        verdicts.append({
            "strategy": strategy,
            "episodes": primary["episodes"],
            "primary_win_rate": primary["episode_win_rate"],
            "primary_median_episode_excess": primary["median_episode_excess"],
            "prehistory_win_rate": primary["prehistory_win_rate"],
            "modern_win_rate": primary["modern_win_rate"],
            "delayed_20bps_win_rate": delayed["episode_win_rate"],
            "delayed_20bps_median_episode_excess": delayed["median_episode_excess"],
            "specialist_gate_pass": passed,
        })
    _write(output / "phase012_acceptance.csv", verdicts)
    summary = {
        "study_id": "BEAR_RECOVERING_SPECIALIST_VALIDATION_PHASE_012",
        "state": STATE,
        "strategies": len(CANDIDATES),
        "specialists_passed": [row["strategy"] for row in verdicts
                               if row["specialist_gate_pass"]],
        "router_effect": "none",
        "paper_trading_effect": "none",
        "generation_015_effect": "none",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "phase012_summary.json").write_text(
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
