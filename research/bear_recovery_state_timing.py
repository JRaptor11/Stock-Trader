"""Phase 013 causal timing audit for the frozen diversified-trend hypothesis."""

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
STRATEGY = "DIVERSIFIED_TREND"
BENCHMARK = "SPY_BUY_HOLD"
COSTS = (10.0, 20.0)
HORIZONS = (1, 3, 5, 10)
SOURCES = (
    ("prehistory", "2008-03-14", "2010-12-31"),
    ("modern", "2012-01-01", "9999-12-31"),
)


def _rows(archive: Path, name: str) -> list[dict]:
    with zipfile.ZipFile(archive) as bundle:
        return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8-sig"))))


def _paths(rows: list[dict]) -> dict[tuple[str, float], dict[str, float]]:
    grouped: dict[tuple[str, float], list[dict]] = defaultdict(list)
    for row in rows:
        cost = float(row["cost_bps"])
        if cost in COSTS and row["strategy"] in (STRATEGY, BENCHMARK):
            grouped[(row["strategy"], cost)].append(row)
    output = {}
    for key, values in grouped.items():
        values.sort(key=lambda row: row["date"])
        previous = 100000.0
        returns = {}
        for row in values:
            equity = float(row["equity"])
            returns[row["date"]] = equity / previous - 1.0
            previous = equity
        output[key] = returns
    return output


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


def _events(labels: dict[str, dict], dates: list[str]) -> list[dict]:
    events = []
    for index, day in enumerate(dates):
        row = labels.get(day)
        if not row:
            continue
        prior = labels.get(dates[index - 1]) if index else None
        raw_onset = (
            row["raw_trend_state"] == STATE and
            row["trend_state"] != STATE and
            (not prior or prior["raw_trend_state"] != STATE)
        )
        confirmed_onset = (
            row["trend_state"] == STATE and
            (not prior or prior["trend_state"] != STATE)
        )
        if raw_onset:
            forward = dates[index + 1:index + 6]
            confirmed_day = next(
                (candidate for candidate in forward
                 if labels.get(candidate, {}).get("trend_state") == STATE), None
            )
            events.append({
                "signal": "RAW_ONSET_UNCONFIRMED",
                "signal_date": day,
                "confirmed_within_5_sessions": bool(confirmed_day),
                "confirmation_date": confirmed_day or "",
                "confirmation_delay_sessions": (
                    dates.index(confirmed_day) - index if confirmed_day else None
                ),
            })
        if confirmed_onset:
            events.append({
                "signal": "CONFIRMED_ONSET",
                "signal_date": day,
                "confirmed_within_5_sessions": True,
                "confirmation_date": day,
                "confirmation_delay_sessions": 0,
            })
    return events


def build_validation(prehistory: Path, modern: Path, output: Path) -> dict:
    archives = {"prehistory": prehistory, "modern": modern}
    event_rows = []
    for source, start, end in SOURCES:
        paths = _paths(_rows(archives[source], "tier1_daily.csv"))
        labels = {row["date"]: row for row in
                  _rows(archives[source], "tier1_market_state_labels.csv")
                  if start <= row["date"] <= end}
        dates = sorted(day for day in paths[(BENCHMARK, 10.0)] if start <= day <= end)
        position = {day: index for index, day in enumerate(dates)}
        for event_number, event in enumerate(_events(labels, dates), 1):
            signal_index = position[event["signal_date"]]
            for cost in COSTS:
                for horizon in HORIZONS:
                    return_dates = dates[signal_index + 1:signal_index + 1 + horizon]
                    if len(return_dates) != horizon:
                        continue
                    strategy_return = _compound(
                        [paths[(STRATEGY, cost)][day] for day in return_dates]
                    )
                    spy_return = _compound(
                        [paths[(BENCHMARK, cost)][day] for day in return_dates]
                    )
                    relative = (1 + strategy_return) / (1 + spy_return) - 1
                    event_rows.append({
                        "source": source,
                        "event_id": f"{source}-{event_number:03d}",
                        **event,
                        "cost_bps": cost,
                        "horizon_sessions": horizon,
                        "return_start": return_dates[0],
                        "return_end": return_dates[-1],
                        "strategy_return": strategy_return,
                        "spy_return": spy_return,
                        "relative_wealth_vs_spy": relative,
                        "beat_spy": relative > 0,
                    })
    _write(output / "phase013_event_scorecard.csv", event_rows)

    aggregate_rows = []
    for signal in ("RAW_ONSET_UNCONFIRMED", "CONFIRMED_ONSET"):
        for cost in COSTS:
            for horizon in HORIZONS:
                selected = [row for row in event_rows if row["signal"] == signal and
                            row["cost_bps"] == cost and
                            row["horizon_sessions"] == horizon]
                relative = [row["relative_wealth_vs_spy"] for row in selected]
                by_source = {}
                for source, _, _ in SOURCES:
                    subset = [row for row in selected if row["source"] == source]
                    by_source[source] = (sum(row["beat_spy"] for row in subset) /
                                         len(subset) if subset else None)
                aggregate_rows.append({
                    "signal": signal,
                    "cost_bps": cost,
                    "horizon_sessions": horizon,
                    "events": len(selected),
                    "episode_win_rate": (sum(row["beat_spy"] for row in selected) /
                                         len(selected) if selected else None),
                    "median_event_excess": statistics.median(relative) if relative else None,
                    "mean_event_excess": statistics.fmean(relative) if relative else None,
                    "worst_event_excess": min(relative) if relative else None,
                    "prehistory_win_rate": by_source["prehistory"],
                    "modern_win_rate": by_source["modern"],
                })
    _write(output / "phase013_signal_scorecard.csv", aggregate_rows)

    raw_events = {}
    for row in event_rows:
        if row["signal"] == "RAW_ONSET_UNCONFIRMED" and row["cost_bps"] == 10.0 and row["horizon_sessions"] == 1:
            raw_events[row["event_id"]] = row
    confirmed = [row for row in raw_events.values() if row["confirmed_within_5_sessions"]]
    delay_rows = [{
        "raw_events": len(raw_events),
        "confirmed_within_5_sessions": len(confirmed),
        "false_start_rate": (1 - len(confirmed) / len(raw_events) if raw_events else None),
        "median_confirmation_delay_sessions": (
            statistics.median(float(row["confirmation_delay_sessions"]) for row in confirmed)
            if confirmed else None
        ),
        "maximum_confirmation_delay_sessions": (
            max(float(row["confirmation_delay_sessions"]) for row in confirmed)
            if confirmed else None
        ),
    }]
    _write(output / "phase013_confirmation_delay.csv", delay_rows)

    def row_for(signal: str, cost: float, horizon: int) -> dict:
        return next(row for row in aggregate_rows if row["signal"] == signal and
                    row["cost_bps"] == cost and row["horizon_sessions"] == horizon)

    primary_horizons = (3, 5, 10)
    raw_primary = [row_for("RAW_ONSET_UNCONFIRMED", 10.0, horizon)
                   for horizon in primary_horizons]
    raw_stress = [row_for("RAW_ONSET_UNCONFIRMED", 20.0, horizon)
                  for horizon in primary_horizons]
    pass_primary = sum(
        row["events"] >= 12 and row["episode_win_rate"] >= 0.60 and
        row["median_event_excess"] > 0 and row["prehistory_win_rate"] >= 0.50 and
        row["modern_win_rate"] >= 0.50 for row in raw_primary
    ) >= 2
    pass_stress = sum(
        row["episode_win_rate"] >= 0.55 and row["median_event_excess"] > 0
        for row in raw_stress
    ) >= 2
    false_start_pass = bool(delay_rows[0]["false_start_rate"] is not None and
                            delay_rows[0]["false_start_rate"] <= 0.25)
    passed = pass_primary and pass_stress and false_start_pass
    summary = {
        "study_id": "BEAR_RECOVERY_STATE_TIMING_PHASE_013",
        "strategy": STRATEGY,
        "raw_events": len(raw_events),
        "raw_signal_pass": passed,
        "primary_horizon_pass": pass_primary,
        "cost_stress_pass": pass_stress,
        "false_start_pass": false_start_pass,
        "router_effect": "none",
        "paper_trading_effect": "none",
        "generation_015_effect": "none",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "phase013_summary.json").write_text(
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
