"""Frozen allocation-sleeve replication for the uncovered long-term states."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.full_roster_state_reliability import (
    _bh_adjust,
    _binomial_sign_p,
    _compound,
    _daily_returns,
    _episode_metrics,
    _read_csv,
    _sha256,
    _state_episodes,
    _write_csv,
)


def _load_declaration(path: Path) -> dict:
    declaration = json.loads(path.read_text(encoding="utf-8"))
    candidates = declaration["candidates"]
    states = declaration["trend_states"]
    strategies = [candidate["strategy"] for candidate in candidates]
    primary_pairs = [
        (candidate["strategy"], candidate["target_state"])
        for candidate in candidates
    ]
    if len(strategies) != len(set(strategies)):
        raise ValueError("candidate strategies must be unique")
    if len(primary_pairs) != len(set(primary_pairs)):
        raise ValueError("candidate target-state pairs must be unique")
    if len(states) != len(set(states)):
        raise ValueError("trend_states must be unique")
    if any(state not in states for _, state in primary_pairs):
        raise ValueError("every candidate target_state must be declared")
    if declaration["primary_family_size"] != len(primary_pairs):
        raise ValueError("primary_family_size must equal candidate target-state pairs")
    if declaration["descriptive_family_size"] != len(strategies) * len(states):
        raise ValueError("descriptive_family_size must equal candidates times states")
    if declaration["benchmark_strategy"] in strategies:
        raise ValueError("benchmark_strategy must not appear in candidates")
    return declaration


def build(
    declaration_path: Path,
    prehistory: Path,
    modern: Path,
    output: Path,
) -> dict:
    declaration = _load_declaration(declaration_path)
    sources = {"prehistory": prehistory, "modern": modern}
    paths = {
        source: _daily_returns(_read_csv(archive, "tier1_daily.csv"))
        for source, archive in sources.items()
    }
    labels = {
        source: _read_csv(archive, "tier1_market_state_labels.csv")
        for source, archive in sources.items()
    }
    benchmark = declaration["benchmark_strategy"]
    candidates = declaration["candidates"]
    strategies = [candidate["strategy"] for candidate in candidates]
    role_by_strategy = {
        candidate["strategy"]: candidate["role"] for candidate in candidates
    }
    target_by_strategy = {
        candidate["strategy"]: candidate["target_state"] for candidate in candidates
    }
    states = declaration["trend_states"]
    costs = [float(value) for value in declaration["costs_bps"]]
    lags = [int(value) for value in declaration["activation_lags_sessions"]]
    required = {
        (strategy, cost)
        for strategy in [benchmark, *strategies]
        for cost in costs
    }
    for source in sources:
        missing = sorted(required - set(paths[source]))
        if missing:
            raise ValueError(f"{source} archive lacks strategy/cost paths: {missing}")

    episode_cache = {
        (source, state): _state_episodes(labels[source], state)
        for source in sources for state in states
    }
    label_boundaries = {
        source: (
            min(row["date"] for row in source_labels),
            max(row["date"] for row in source_labels),
        )
        for source, source_labels in labels.items()
    }
    episode_rows: list[dict] = []
    excluded_rows: list[dict] = []
    for block in declaration["fixed_calendar_blocks"]:
        block_id, source = block["id"], block["source"]
        start, end = block["start"], block["end"]
        for state in states:
            for episode_number, episode in enumerate(episode_cache[(source, state)], 1):
                if episode[-1] < start or episode[0] > end:
                    continue
                if (episode[0] == label_boundaries[source][0]
                        or episode[-1] == label_boundaries[source][1]):
                    excluded_rows.append({
                        "block": block_id, "source": source, "trend_state": state,
                        "source_episode_number": episode_number,
                        "episode_start": episode[0], "episode_end": episode[-1],
                        "reason": "touches_archive_boundary",
                    })
                    continue
                if episode[0] < start or episode[-1] > end:
                    excluded_rows.append({
                        "block": block_id, "source": source, "trend_state": state,
                        "source_episode_number": episode_number,
                        "episode_start": episode[0], "episode_end": episode[-1],
                        "reason": "crosses_calendar_block_boundary",
                    })
                    continue
                for strategy in strategies:
                    for cost in costs:
                        candidate = paths[source][(strategy, cost)]
                        control = paths[source][(benchmark, cost)]
                        if any(day not in candidate or day not in control for day in episode):
                            if strategy == strategies[0] and cost == costs[0]:
                                excluded_rows.append({
                                    "block": block_id, "source": source,
                                    "trend_state": state,
                                    "source_episode_number": episode_number,
                                    "episode_start": episode[0],
                                    "episode_end": episode[-1],
                                    "reason": "incomplete_strategy_or_benchmark_path",
                                })
                            continue
                        for lag in lags:
                            selected = episode[lag:]
                            if not selected:
                                continue
                            strategy_return = _compound(candidate, selected)
                            spy_return = _compound(control, selected)
                            excess = (1.0 + strategy_return) / (1.0 + spy_return) - 1.0
                            episode_rows.append({
                                "strategy": strategy,
                                "role": role_by_strategy[strategy],
                                "trend_state": state,
                                "predeclared_primary": state == target_by_strategy[strategy],
                                "block": block_id, "source": source,
                                "episode_id": f"{block_id}-{state}-{episode_number:03d}",
                                "episode_start": episode[0], "episode_end": episode[-1],
                                "scored_start": selected[0], "scored_end": selected[-1],
                                "sessions": len(selected), "cost_bps": cost,
                                "activation_lag_sessions": lag,
                                "strategy_return": strategy_return,
                                "spy_return": spy_return,
                                "relative_wealth_vs_spy": excess,
                                "beat_spy": excess > 0.0,
                            })
    _write_csv(output / "allocation_gap_episode_scorecard.csv", episode_rows)
    _write_csv(output / "allocation_gap_excluded_episodes.csv", excluded_rows)

    minimum_block_episodes = int(
        declaration["eligible_block"]["minimum_complete_episodes"]
    )
    minimum_block_sessions = int(
        declaration["eligible_block"]["minimum_scored_sessions"]
    )
    block_rows: list[dict] = []
    for strategy in strategies:
        for state in states:
            for cost in costs:
                for lag in lags:
                    relevant = [
                        row for row in episode_rows
                        if row["strategy"] == strategy
                        and row["trend_state"] == state
                        and row["cost_bps"] == cost
                        and row["activation_lag_sessions"] == lag
                    ]
                    for block in declaration["fixed_calendar_blocks"]:
                        rows = [row for row in relevant if row["block"] == block["id"]]
                        metrics = _episode_metrics(rows)
                        strategy_wealth = spy_wealth = 1.0
                        sessions = 0
                        for row in rows:
                            strategy_wealth *= 1.0 + float(row["strategy_return"])
                            spy_wealth *= 1.0 + float(row["spy_return"])
                            sessions += int(row["sessions"])
                        relative = strategy_wealth / spy_wealth - 1.0 if rows else None
                        block_rows.append({
                            "strategy": strategy,
                            "role": role_by_strategy[strategy],
                            "trend_state": state,
                            "predeclared_primary": state == target_by_strategy[strategy],
                            "block": block["id"], "source": block["source"],
                            "cost_bps": cost, "activation_lag_sessions": lag,
                            "sessions": sessions, **metrics,
                            "block_relative_wealth_vs_spy": relative,
                            "positive_block": relative is not None and relative > 0.0,
                            "eligible_block": (
                                len(rows) >= minimum_block_episodes
                                and sessions >= minimum_block_sessions
                            ),
                        })
    _write_csv(output / "allocation_gap_block_scorecard.csv", block_rows)

    aggregate_rows: list[dict] = []
    for strategy in strategies:
        for state in states:
            for cost in costs:
                for lag in lags:
                    rows = [
                        row for row in episode_rows
                        if row["strategy"] == strategy
                        and row["trend_state"] == state
                        and row["cost_bps"] == cost
                        and row["activation_lag_sessions"] == lag
                    ]
                    eligible = [
                        row for row in block_rows
                        if row["strategy"] == strategy
                        and row["trend_state"] == state
                        and row["cost_bps"] == cost
                        and row["activation_lag_sessions"] == lag
                        and row["eligible_block"]
                    ]
                    metrics = _episode_metrics(rows)
                    positives = sum(bool(row["positive_block"]) for row in eligible)
                    positive_prehistory = sum(
                        bool(row["positive_block"])
                        for row in eligible if row["source"] == "prehistory"
                    )
                    positive_modern = sum(
                        bool(row["positive_block"])
                        for row in eligible if row["source"] == "modern"
                    )
                    aggregate_rows.append({
                        "strategy": strategy,
                        "role": role_by_strategy[strategy],
                        "trend_state": state,
                        "predeclared_primary": state == target_by_strategy[strategy],
                        "cost_bps": cost, "activation_lag_sessions": lag,
                        **metrics,
                        "eligible_blocks": len(eligible),
                        "positive_blocks": positives,
                        "positive_prehistory_blocks": positive_prehistory,
                        "positive_modern_blocks": positive_modern,
                        "positive_block_fraction": (
                            positives / len(eligible) if eligible else None
                        ),
                        "one_sided_episode_sign_p": _binomial_sign_p(
                            metrics["wins"], metrics["episodes"]
                        ),
                    })
    _write_csv(output / "allocation_gap_all_state_scorecard.csv", aggregate_rows)

    primary_cost = float(declaration["primary"]["cost_bps"])
    primary_lag = int(declaration["primary"]["activation_lag_sessions"])
    stress_cost = float(declaration["stress"]["cost_bps"])
    stress_lag = int(declaration["stress"]["activation_lag_sessions"])
    primary_rows = [
        row for row in aggregate_rows
        if row["predeclared_primary"]
        and row["cost_bps"] == primary_cost
        and row["activation_lag_sessions"] == primary_lag
    ]
    if len(primary_rows) != declaration["primary_family_size"]:
        raise ValueError("primary inference family is incomplete")
    gates = declaration["reliability_gate"]
    inference_p_values = [
        float(row["one_sided_episode_sign_p"])
        if row["episodes"] >= gates["minimum_complete_episodes"] else 1.0
        for row in primary_rows
    ]
    q_values = _bh_adjust(inference_p_values)
    acceptance_rows: list[dict] = []
    for primary, inference_p, q_value in zip(
        primary_rows, inference_p_values, q_values
    ):
        stress = next(
            row for row in aggregate_rows
            if row["strategy"] == primary["strategy"]
            and row["trend_state"] == primary["trend_state"]
            and row["cost_bps"] == stress_cost
            and row["activation_lag_sessions"] == stress_lag
        )
        checks = {
            "episodes_pass": primary["episodes"] >= gates["minimum_complete_episodes"],
            "eligible_blocks_pass": primary["eligible_blocks"] >= gates["minimum_eligible_blocks"],
            "positive_blocks_pass": (
                primary["positive_block_fraction"] is not None
                and primary["positive_block_fraction"] >= gates["minimum_positive_block_fraction"]
            ),
            "prehistory_corroboration_pass": (
                primary["positive_prehistory_blocks"]
                >= gates["minimum_positive_prehistory_blocks"]
            ),
            "modern_corroboration_pass": (
                primary["positive_modern_blocks"]
                >= gates["minimum_positive_modern_blocks"]
            ),
            "primary_win_rate_pass": (
                primary["episode_win_rate"] is not None
                and primary["episode_win_rate"] >= gates["minimum_primary_episode_win_rate"]
            ),
            "primary_median_pass": (
                primary["median_episode_excess"] is not None
                and primary["median_episode_excess"] > 0.0
            ),
            "primary_profit_factor_pass": (
                primary["profit_factor"] is not None
                and primary["profit_factor"] >= gates["minimum_primary_profit_factor"]
            ),
            "multiplicity_pass": q_value <= gates["maximum_benjamini_hochberg_q"],
            "stress_win_rate_pass": (
                stress["episode_win_rate"] is not None
                and stress["episode_win_rate"] >= gates["minimum_stress_episode_win_rate"]
            ),
            "stress_median_pass": (
                stress["median_episode_excess"] is not None
                and stress["median_episode_excess"] > 0.0
            ),
        }
        acceptance_rows.append({
            "strategy": primary["strategy"], "role": primary["role"],
            "target_state": primary["trend_state"],
            "primary_episodes": primary["episodes"],
            "primary_eligible_blocks": primary["eligible_blocks"],
            "primary_positive_blocks": primary["positive_blocks"],
            "primary_positive_block_fraction": primary["positive_block_fraction"],
            "primary_positive_prehistory_blocks": primary["positive_prehistory_blocks"],
            "primary_positive_modern_blocks": primary["positive_modern_blocks"],
            "primary_episode_win_rate": primary["episode_win_rate"],
            "primary_median_episode_excess": primary["median_episode_excess"],
            "primary_mean_episode_excess": primary["mean_episode_excess"],
            "primary_profit_factor": primary["profit_factor"],
            "primary_worst_episode_excess": primary["worst_episode_excess"],
            "primary_sign_p": primary["one_sided_episode_sign_p"],
            "primary_family_inference_p": inference_p,
            "primary_bh_q": q_value,
            "stress_episode_win_rate": stress["episode_win_rate"],
            "stress_median_episode_excess": stress["median_episode_excess"],
            "stress_profit_factor": stress["profit_factor"],
            **checks,
            "criteria_passed": sum(checks.values()),
            "criteria_total": len(checks),
            "reliability_gate_pass": all(checks.values()),
        })
    _write_csv(output / "allocation_gap_primary_acceptance.csv", acceptance_rows)

    ranked = sorted(
        acceptance_rows,
        key=lambda row: (
            -int(row["criteria_passed"]), float(row["primary_bh_q"]),
            -float(row["primary_episode_win_rate"] or 0.0), row["strategy"],
        ),
    )
    summary = {
        "study_id": declaration["study_id"],
        "primary_family_size": declaration["primary_family_size"],
        "descriptive_family_size": declaration["descriptive_family_size"],
        "candidates": len(strategies), "trend_states": len(states),
        "complete_episode_rows": len(episode_rows),
        "excluded_episode_records": len(excluded_rows),
        "passed": [
            {"strategy": row["strategy"], "target_state": row["target_state"]}
            for row in acceptance_rows if row["reliability_gate_pass"]
        ],
        "closest_primary_cells_descriptive_only": [
            {
                "strategy": row["strategy"], "target_state": row["target_state"],
                "criteria_passed": row["criteria_passed"],
                "criteria_total": row["criteria_total"],
                "primary_bh_q": row["primary_bh_q"],
            }
            for row in ranked
        ],
        "source_sha256": {source: _sha256(path) for source, path in sources.items()},
        "declaration_sha256": _sha256(declaration_path),
        "generation_015_effect": "none", "router_effect": "none",
        "paper_trading_effect": "none", "live_trading_effect": "none",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "allocation_gap_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    (output / "frozen_declaration.json").write_text(
        json.dumps(declaration, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--declaration", type=Path,
        default=Path(__file__).with_name(
            "long-term-allocation-gap-replication-001.json"
        ),
    )
    parser.add_argument("--prehistory", type=Path, required=True)
    parser.add_argument("--modern", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(
        build(args.declaration, args.prehistory, args.modern, args.output),
        indent=2,
    ))


if __name__ == "__main__":
    main()
