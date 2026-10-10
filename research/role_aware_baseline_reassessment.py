"""Role-aware secondary reassessment of immutable Phase 011 evidence."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path


COSTS = (10.0, 20.0)
BENCHMARK = "SPY_BUY_HOLD"


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _bool(value: str) -> bool:
    return value.lower() == "true"


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def build_reassessment(phase011: Path, phase012: Path, phase013: Path,
                       output: Path) -> dict:
    eras = _rows(phase011 / "phase011_era_scorecard.csv")
    specialists = _rows(phase011 / "phase011_state_specialist_acceptance.csv")
    phase012_summary = json.loads((phase012 / "phase012_summary.json").read_text())
    phase013_summary = json.loads((phase013 / "phase013_summary.json").read_text())
    later_rejected = set()
    if not phase012_summary["specialists_passed"]:
        later_rejected.update({
            "CROSS_ASSET_DUAL_MOMENTUM",
            "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE",
            "DIVERSIFIED_TREND",
        })
    if not phase013_summary["raw_signal_pass"]:
        later_rejected.add("DIVERSIFIED_TREND")

    strategies = sorted({row["strategy"] for row in eras if row["strategy"] != BENCHMARK})
    role_rows = []
    for strategy in strategies:
        cost_metrics = {}
        for cost in COSTS:
            selected = [row for row in eras if row["strategy"] == strategy and
                        float(row["cost_bps"]) == cost]
            relative = [float(row["relative_wealth_vs_spy"]) for row in selected]
            retention = [float(row["spy_cagr_retention"]) for row in selected
                         if row["spy_cagr_retention"] not in ("", "None")]
            cost_metrics[cost] = {
                "blocks": len(selected),
                "blocks_beating_spy": sum(_bool(row["beat_spy"]) for row in selected),
                "median_relative_wealth": statistics.median(relative),
                "worst_relative_wealth": min(relative),
                "blocks_lower_drawdown": sum(_bool(row["drawdown_better_than_spy"])
                                               for row in selected),
                "blocks_higher_sharpe": sum(_bool(row["sharpe_better_than_spy"])
                                              for row in selected),
                "median_spy_cagr_retention": statistics.median(retention) if retention else None,
            }
        def passes(role: str, metric: dict) -> bool:
            if role == "growth_baseline":
                return (metric["blocks"] == 6 and metric["blocks_beating_spy"] >= 3 and
                        metric["median_relative_wealth"] > 0 and
                        metric["worst_relative_wealth"] >= -0.10 and
                        metric["median_spy_cagr_retention"] >= 0.90)
            if role == "stability_baseline":
                return (metric["blocks"] == 6 and metric["blocks_lower_drawdown"] >= 6 and
                        metric["blocks_higher_sharpe"] >= 4 and
                        metric["median_spy_cagr_retention"] >= 0.65 and
                        metric["worst_relative_wealth"] >= -0.25)
            return (metric["blocks"] == 6 and metric["blocks_lower_drawdown"] >= 5 and
                    metric["blocks_higher_sharpe"] >= 2 and
                    metric["median_spy_cagr_retention"] >= 0.80 and
                    metric["worst_relative_wealth"] >= -0.25)
        for role in ("growth_baseline", "stability_baseline", "capital_retention_control"):
            primary = cost_metrics[10.0]
            cost_pass = {cost: passes(role, metric) for cost, metric in cost_metrics.items()}
            role_rows.append({
                "strategy": strategy, "role": role,
                **primary,
                "pass_10bps": cost_pass[10.0], "pass_20bps": cost_pass[20.0],
                "role_fit": all(cost_pass.values()),
                "effect": "nomination_only" if all(cost_pass.values()) else "none",
            })

    specialist_rows = []
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in specialists:
        grouped[(row["strategy"], row["trend_state"])].append(row)
    for (strategy, state), rows in sorted(grouped.items()):
        row = rows[0]
        base_pass = (int(row["eligible_blocks"]) >= 3 and
                     int(row["positive_relative_blocks"]) >= 2 and
                     float(row["aggregate_episode_win_rate"]) >= 0.60)
        later_rejection = strategy in later_rejected and state == "BEAR_RECOVERING"
        specialist_rows.append({
            "strategy": strategy, "trend_state": state,
            "eligible_blocks": row["eligible_blocks"],
            "positive_relative_blocks": row["positive_relative_blocks"],
            "aggregate_episodes": row["aggregate_episodes"],
            "aggregate_episode_win_rate": row["aggregate_episode_win_rate"],
            "phase011_role_fit": base_pass,
            "later_dedicated_rejection": later_rejection,
            "conditional_role_fit": base_pass and not later_rejection,
        })

    _write(output / "role_aware_baseline_scorecard.csv", role_rows)
    _write(output / "role_aware_specialist_scorecard.csv", specialist_rows)
    fits = [{"strategy": row["strategy"], "role": row["role"]}
            for row in role_rows if row["role_fit"]]
    conditional = [{"strategy": row["strategy"], "state": row["trend_state"]}
                   for row in specialist_rows if row["conditional_role_fit"]]
    summary = {
        "study_id": "ROLE_AWARE_BASELINE_REASSESSMENT_STUDY_001",
        "role_fits": fits,
        "conditional_role_fits": conditional,
        "router_effect": "none", "paper_trading_effect": "none",
        "promotion_effect": "none", "generation_015_effect": "none",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "role_aware_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase011", type=Path, required=True)
    parser.add_argument("--phase012", type=Path, required=True)
    parser.add_argument("--phase013", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_reassessment(args.phase011, args.phase012, args.phase013,
                                        args.output), indent=2))


if __name__ == "__main__":
    main()
