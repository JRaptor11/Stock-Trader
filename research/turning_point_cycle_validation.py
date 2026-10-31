"""Combine immutable turning-point archives without changing frozen strategies."""

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


def _read_csv(archive: Path, name: str) -> list[dict]:
    with zipfile.ZipFile(archive) as bundle:
        return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8-sig"))))


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _true(value: object) -> bool:
    return str(value).strip().lower() == "true"


def _metrics(values: list[float]) -> dict:
    wins = sum(value > 0.0 for value in values)
    gains = sum(value for value in values if value > 0.0)
    losses = -sum(value for value in values if value < 0.0)
    return {
        "episodes": len(values),
        "wins": wins,
        "episode_win_rate": wins / len(values) if values else None,
        "median_episode_excess": statistics.median(values) if values else None,
        "mean_episode_excess": statistics.fmean(values) if values else None,
        "profit_factor": gains / losses if losses else (math.inf if gains else None),
        "worst_episode_excess": min(values) if values else None,
        "best_episode_excess": max(values) if values else None,
    }


def _sign_p(wins: int, observations: int) -> float:
    if observations <= 0:
        return 1.0
    return min(1.0, sum(
        math.comb(observations, value)
        for value in range(wins, observations + 1)
    ) / (2 ** observations))


def _bh(values: list[float]) -> list[float]:
    count = len(values)
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    adjusted = [1.0] * count
    running = 1.0
    for index in range(count - 1, -1, -1):
        original, value = ordered[index]
        running = min(running, value * count / (index + 1))
        adjusted[original] = min(1.0, running)
    return adjusted


def _daily_returns(rows: list[dict], initial_cash: float) -> dict[tuple[str, float], dict[str, float]]:
    grouped: dict[tuple[str, float], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["strategy"], float(row["cost_bps"]))].append(row)
    result = {}
    for key, values in grouped.items():
        previous = initial_cash
        path = {}
        for row in sorted(values, key=lambda item: item["date"]):
            equity = float(row["equity"])
            path[row["date"]] = equity / previous - 1.0
            previous = equity
        result[key] = path
    return result


def _compound(path: dict[str, float], dates: list[str]) -> float:
    wealth = 1.0
    for day in dates:
        wealth *= 1.0 + path[day]
    return wealth - 1.0


def analyze(archives: dict[str, Path], output: Path, primary_cost_bps: float = 10.0) -> dict:
    whole_path: list[dict] = []
    episode_values: dict[tuple[str, str, float, str], list[float]] = defaultdict(list)
    canonical_rows: list[dict] = []
    block_rows: list[dict] = []
    archive_manifest: list[dict] = []

    for source, archive in sorted(archives.items()):
        with zipfile.ZipFile(archive) as bundle:
            required = {
                "tier1_manifest.json", "tier1_cost_ladder_scorecard.csv",
                "tier1_turning_point_cycle_episode_returns.csv",
                "tier1_market_state_hierarchy_period_scorecard.csv",
                "tier1_turning_point_cycle_labels.csv", "tier1_daily.csv",
            }
            missing = sorted(required - set(bundle.namelist()))
            if missing:
                raise ValueError(f"{source} archive lacks turning-point evidence: {missing}")
            manifest = json.loads(bundle.read("tier1_manifest.json"))
        diagnostics = manifest.get("turning_point_cycle_diagnostics", {})
        if diagnostics.get("enabled") is not True:
            raise ValueError(f"{source} turning-point diagnostics are not enabled")
        archive_manifest.append({
            "source": source,
            "archive": str(archive.resolve()),
            "source_sha256": manifest.get("source_sha256"),
            "scored_start": manifest.get("coverage", {}).get("scored_start"),
            "scored_end": manifest.get("coverage", {}).get("scored_end"),
            "scored_sessions": manifest.get("coverage", {}).get("scored_sessions"),
        })

        scorecards = _read_csv(archive, "tier1_cost_ladder_scorecard.csv")
        benchmark = {
            float(row["cost_bps"]): float(row["total_return"])
            for row in scorecards if row["strategy"] == "SPY_BUY_HOLD"
        }
        for row in scorecards:
            cost = float(row["cost_bps"])
            total = float(row["total_return"])
            whole_path.append({
                "source": source,
                "strategy": row["strategy"],
                "cost_bps": cost,
                "total_return": total,
                "cagr": float(row["cagr"]),
                "annualized_volatility": float(row["annualized_volatility"]),
                "sharpe": float(row["sharpe"]),
                "max_drawdown": float(row["max_drawdown"]),
                "relative_wealth_vs_spy": (1.0 + total) / (1.0 + benchmark[cost]) - 1.0,
                "turnover": float(row["turnover"]),
                "trade_count": int(row["trade_count"]),
            })

        for row in _read_csv(archive, "tier1_turning_point_cycle_episode_returns.csv"):
            if not _true(row["complete_episode"]):
                continue
            key = (source, row["strategy"], float(row["cost_bps"]), row["turning_point_cycle"])
            episode_values[key].append(float(row["relative_wealth_vs_benchmark"]))

        labels = {
            row["date"]: row["turning_point_cycle"]
            for row in _read_csv(archive, "tier1_turning_point_cycle_labels.csv")
        }
        paths = _daily_returns(
            _read_csv(archive, "tier1_daily.csv"),
            float(manifest.get("config", {}).get("initial_cash", 100000.0)),
        )
        for (strategy, cost), path in sorted(paths.items()):
            benchmark_path = paths.get(("SPY_BUY_HOLD", cost), {})
            grouped_dates: dict[tuple[str, str], list[str]] = defaultdict(list)
            for day in sorted(set(path) & set(benchmark_path) & set(labels)):
                year = int(day[:4])
                block_start = year - (year % 2)
                block = f"{block_start}-{block_start + 1}"
                grouped_dates[(labels[day], block)].append(day)
            for (cycle, block), days in sorted(grouped_dates.items()):
                strategy_return = _compound(path, days)
                benchmark_return = _compound(benchmark_path, days)
                block_rows.append({
                    "source": source, "strategy": strategy, "cost_bps": cost,
                    "turning_point_cycle": cycle, "calendar_block": block,
                    "sessions": len(days), "strategy_return": strategy_return,
                    "benchmark_return": benchmark_return,
                    "relative_wealth_vs_benchmark": (
                        (1.0 + strategy_return) / (1.0 + benchmark_return) - 1.0
                    ),
                })

        for row in _read_csv(archive, "tier1_market_state_hierarchy_period_scorecard.csv"):
            if (row["state_level"] != "trend" or row["period"] != "full"
                    or float(row["cost_bps"]) != primary_cost_bps
                    or not row["strategy"].startswith("TURNING_POINT_")):
                continue
            canonical_rows.append({
                "source": source,
                "strategy": row["strategy"],
                "trend_state": row["state_key"],
                "sessions": int(row["sessions"]),
                "episodes": int(row["episodes"]),
                "sample_sufficient": _true(row["sample_sufficient"]),
                "relative_wealth_vs_spy": float(row["relative_wealth_vs_spy"]),
                "episode_beat_spy_rate": float(row["episode_beat_spy_rate"]),
                "conditional_sequence_max_drawdown": float(row["conditional_sequence_max_drawdown"]),
            })

    source_summary: list[dict] = []
    for (source, strategy, cost, cycle), values in sorted(episode_values.items()):
        metrics = _metrics(values)
        source_summary.append({
            "source": source, "strategy": strategy, "cost_bps": cost,
            "turning_point_cycle": cycle, **metrics,
            "one_sided_sign_p": _sign_p(metrics["wins"], metrics["episodes"]),
        })

    combined_values: dict[tuple[str, float, str], list[float]] = defaultdict(list)
    for (_, strategy, cost, cycle), values in episode_values.items():
        combined_values[(strategy, cost, cycle)].extend(values)
    combined_summary: list[dict] = []
    for (strategy, cost, cycle), values in sorted(combined_values.items()):
        metrics = _metrics(values)
        sources = [
            row for row in source_summary
            if row["strategy"] == strategy and row["cost_bps"] == cost
            and row["turning_point_cycle"] == cycle
        ]
        combined_summary.append({
            "strategy": strategy, "cost_bps": cost,
            "turning_point_cycle": cycle, **metrics,
            "one_sided_sign_p": _sign_p(metrics["wins"], metrics["episodes"]),
            "positive_median_sources": sum(
                (row["median_episode_excess"] or 0.0) > 0.0 for row in sources
            ),
            "positive_profit_factor_sources": sum(
                (row["profit_factor"] or 0.0) > 1.0 for row in sources
            ),
            "source_count": len(sources),
        })

    family = [
        row for row in combined_summary
        if row["strategy"].startswith("TURNING_POINT_")
    ]
    q_values = _bh([float(row["one_sided_sign_p"]) for row in family])
    for row, q_value in zip(family, q_values):
        row["family_bh_q"] = q_value
    for row in combined_summary:
        row.setdefault("family_bh_q", None)

    block_summary: list[dict] = []
    grouped_blocks: dict[tuple[str, float, str], list[dict]] = defaultdict(list)
    for row in block_rows:
        grouped_blocks[(
            row["strategy"], float(row["cost_bps"]), row["turning_point_cycle"]
        )].append(row)
    for (strategy, cost, cycle), rows in sorted(grouped_blocks.items()):
        values = [float(row["relative_wealth_vs_benchmark"]) for row in rows]
        block_summary.append({
            "strategy": strategy, "cost_bps": cost,
            "turning_point_cycle": cycle, "blocks": len(values),
            "positive_blocks": sum(value > 0.0 for value in values),
            "positive_block_rate": sum(value > 0.0 for value in values) / len(values),
            "median_block_excess": statistics.median(values),
            "worst_block_excess": min(values),
            "best_block_excess": max(values),
        })

    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "turning_point_whole_path.csv", whole_path)
    _write_csv(output / "turning_point_cycle_source_summary.csv", source_summary)
    _write_csv(output / "turning_point_cycle_combined_summary.csv", combined_summary)
    _write_csv(output / "turning_point_cycle_calendar_blocks.csv", block_rows)
    _write_csv(output / "turning_point_cycle_block_summary.csv", block_summary)
    _write_csv(output / "turning_point_canonical_state_summary.csv", canonical_rows)
    payload = {
        "status": "descriptive_two_era_validation_complete",
        "primary_cost_bps": primary_cost_bps,
        "archives": archive_manifest,
        "selection_effect": "none",
        "router_effect": "none",
        "paper_or_live_effect": "none",
        "warning": (
            "The frozen declaration specified acceptance concepts but not numeric "
            "thresholds. Results are therefore descriptive and cannot be declared "
            "accepted by a post-hoc numeric gate."
        ),
    }
    (output / "analysis.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    primary_whole = [row for row in whole_path if row["cost_bps"] == primary_cost_bps]
    primary_cycles = [
        row for row in combined_summary
        if row["cost_bps"] == primary_cost_bps
        and row["strategy"].startswith("TURNING_POINT_")
    ]
    primary_blocks = {
        (row["strategy"], row["turning_point_cycle"]): row
        for row in block_summary
        if row["cost_bps"] == primary_cost_bps
    }
    lines = [
        "# Novel downside mechanisms: two-era validation", "",
        payload["warning"], "", "## Whole-path results at 10 bps", "",
        "| Source | Strategy | CAGR | Sharpe | Max drawdown | Relative wealth vs SPY |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in primary_whole:
        lines.append(
            f"| {row['source']} | {row['strategy']} | {row['cagr']:.2%} | "
            f"{row['sharpe']:.2f} | {row['max_drawdown']:.2%} | "
            f"{row['relative_wealth_vs_spy']:.2%} |"
        )
    lines.extend([
        "", "## Complete turning-point episodes combined across eras at 10 bps", "",
        "| Strategy | Cycle | Episodes | Win rate | Median excess | Profit factor | Positive eras | Positive 2y blocks | BH q |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for row in primary_cycles:
        profit = row["profit_factor"]
        block = primary_blocks[(row["strategy"], row["turning_point_cycle"])]
        lines.append(
            f"| {row['strategy']} | {row['turning_point_cycle']} | {row['episodes']} | "
            f"{row['episode_win_rate']:.1%} | {row['median_episode_excess']:.2%} | "
            f"{profit:.2f} | {row['positive_median_sources']}/{row['source_count']} | "
            f"{block['positive_blocks']}/{block['blocks']} | "
            f"{row['family_bh_q']:.3f} |"
        )
    lines.extend([
        "", "## Interpretation guardrails", "",
        "- These are isolated mechanisms, not a router and not trading approval.",
        "- Boundary-truncated episodes are excluded from recurrence statistics.",
        "- Cycle labels are causal: only the prior completed close is used.",
        "- Family BH correction covers all four candidates, four cycles, and four cost levels.",
        "- Numeric acceptance thresholds must be frozen in a successor declaration before any further return analysis.",
    ])
    (output / "analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="append", required=True, metavar="NAME=PATH")
    parser.add_argument("--output", required=True)
    parser.add_argument("--primary-cost-bps", type=float, default=10.0)
    args = parser.parse_args(argv)
    archives = {}
    for value in args.archive:
        name, separator, path = value.partition("=")
        if not separator or not name or not path:
            parser.error("--archive must use NAME=PATH")
        archives[name] = Path(path)
    analyze(archives, Path(args.output), args.primary_cost_bps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
