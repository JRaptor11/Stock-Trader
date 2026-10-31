"""Frozen full-roster state reliability screen over immutable Phase 010 paths."""

from __future__ import annotations

import argparse
import csv
import hashlib
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _daily_returns(rows: list[dict]) -> dict[tuple[str, float], dict[str, float]]:
    grouped: dict[tuple[str, float], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["strategy"], float(row["cost_bps"]))].append(row)
    result = {}
    for key, values in grouped.items():
        values.sort(key=lambda row: row["date"])
        previous = 100000.0
        path = {}
        for row in values:
            equity = float(row["equity"])
            path[row["date"]] = equity / previous - 1.0
            previous = equity
        result[key] = path
    return result


def _dominant_holding_by_date(rows: list[dict], trades: list[dict],
                              strategy: str, cost_bps: float) -> dict[str, str]:
    """Reconstruct the dominant newly purchased holding after each open."""
    dates = sorted(
        row["date"] for row in rows
        if row["strategy"] == strategy and float(row["cost_bps"]) == cost_bps
    )
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in trades:
        if row["strategy"] == strategy and float(row["cost_bps"]) == cost_bps:
            by_date[row["date"]].append(row)
    current = ""
    result = {}
    for day in dates:
        buys = [row for row in by_date.get(day, []) if float(row["notional"]) > 0.0]
        if buys:
            current = max(buys, key=lambda row: float(row["notional"]))["symbol"]
        result[day] = current
    return result


def _state_episodes(labels: list[dict], state: str) -> list[list[str]]:
    """Build source-wide episodes before applying archive and era boundaries."""
    episodes: list[list[str]] = []
    current: list[str] = []
    for row in sorted(labels, key=lambda item: item["date"]):
        if row.get("trend_state") == state:
            current.append(row["date"])
        elif current:
            episodes.append(current)
            current = []
    if current:
        episodes.append(current)
    return episodes


def _compound(path: dict[str, float], dates: list[str]) -> float:
    wealth = 1.0
    for day in dates:
        wealth *= 1.0 + path[day]
    return wealth - 1.0


def _path_metrics(returns: list[float]) -> dict:
    wealth = peak = 1.0
    maximum_drawdown = 0.0
    for value in returns:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        maximum_drawdown = min(maximum_drawdown, wealth / peak - 1.0)
    sessions = len(returns)
    volatility = statistics.pstdev(returns) * math.sqrt(252.0) if returns else 0.0
    return {
        "sessions": sessions,
        "total_return": wealth - 1.0,
        "cagr": wealth ** (252.0 / sessions) - 1.0 if sessions else 0.0,
        "annualized_volatility": volatility,
        "sharpe": (
            statistics.fmean(returns) * 252.0 / volatility
            if volatility > 0.0 else 0.0
        ),
        "max_drawdown": maximum_drawdown,
    }


def _episode_metrics(rows: list[dict]) -> dict:
    values = [float(row["relative_wealth_vs_spy"]) for row in rows]
    wins = sum(value > 0.0 for value in values)
    gains = sum(value for value in values if value > 0.0)
    losses = -sum(value for value in values if value < 0.0)
    positive = [value for value in values if value > 0.0]
    negative = [-value for value in values if value < 0.0]
    streak = maximum_streak = 0
    for value in values:
        streak = streak + 1 if value <= 0.0 else 0
        maximum_streak = max(maximum_streak, streak)
    return {
        "episodes": len(rows),
        "wins": wins,
        "episode_win_rate": wins / len(rows) if rows else None,
        "median_episode_excess": statistics.median(values) if values else None,
        "mean_episode_excess": statistics.fmean(values) if values else None,
        "profit_factor": gains / losses if losses else (math.inf if gains else None),
        "payoff_ratio": (
            statistics.fmean(positive) / statistics.fmean(negative)
            if positive and negative else None
        ),
        "worst_episode_excess": min(values) if values else None,
        "best_episode_excess": max(values) if values else None,
        "maximum_consecutive_losses": maximum_streak,
    }


def _binomial_sign_p(wins: int, observations: int) -> float:
    if observations <= 0:
        return 1.0
    return min(
        1.0,
        sum(math.comb(observations, value)
            for value in range(wins, observations + 1)) / (2 ** observations),
    )


def _bh_adjust(p_values: list[float]) -> list[float]:
    count = len(p_values)
    ordered = sorted(enumerate(p_values), key=lambda item: (item[1], item[0]))
    adjusted = [1.0] * count
    running = 1.0
    for index in range(count - 1, -1, -1):
        original, value = ordered[index]
        running = min(running, value * count / (index + 1))
        adjusted[original] = min(1.0, running)
    return adjusted


def _load_declaration(path: Path) -> dict:
    declaration = json.loads(path.read_text(encoding="utf-8"))
    strategies = declaration["strategies"]
    states = declaration["trend_states"]
    if len(strategies) != len(set(strategies)) or len(states) != len(set(states)):
        raise ValueError("strategies and trend_states must be unique")
    if declaration["family_size"] != len(strategies) * len(states):
        raise ValueError("family_size must equal strategies times trend_states")
    if declaration["benchmark_strategy"] in strategies:
        raise ValueError("benchmark_strategy must not appear in strategies")
    return declaration


def build(
    declaration_path: Path,
    prehistory: Path | None,
    modern: Path | None,
    output: Path,
    archives: dict[str, Path] | None = None,
) -> dict:
    declaration = _load_declaration(declaration_path)
    if archives is None:
        if prehistory is None or modern is None:
            raise ValueError("prehistory and modern archives are required")
        sources = {"prehistory": prehistory, "modern": modern}
    else:
        sources = dict(archives)
        if not sources:
            raise ValueError("at least one named archive is required")
    strategy_sources = {
        strategy: set(declaration.get("strategy_sources", {}).get(strategy, sources))
        for strategy in declaration["strategies"]
    }
    unknown_sources = sorted(
        {source for assigned in strategy_sources.values() for source in assigned} - set(sources)
    )
    if unknown_sources:
        raise ValueError(f"strategy_sources references unknown archives: {unknown_sources}")
    daily_rows = {
        source: _read_csv(archive, "tier1_daily.csv")
        for source, archive in sources.items()
    }
    paths = {
        source: _daily_returns(rows) for source, rows in daily_rows.items()
    }
    labels = {
        source: _read_csv(archive, "tier1_market_state_labels.csv")
        for source, archive in sources.items()
    }
    benchmark = declaration["benchmark_strategy"]
    strategies = declaration["strategies"]
    states = declaration["trend_states"]
    costs = [float(value) for value in declaration["costs_bps"]]
    lags = [int(value) for value in declaration["activation_lags_sessions"]]
    for source in sources:
        assigned = [strategy for strategy in strategies if source in strategy_sources[strategy]]
        required = {
            (strategy, cost) for strategy in [benchmark, *assigned] for cost in costs
        }
        missing = sorted(required - set(paths[source]))
        if missing:
            raise ValueError(f"{source} archive lacks strategy/cost paths: {missing}")

    role_contracts = {
        row["strategy"]: row for row in declaration.get("strategy_role_contracts", [])
    }
    role_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    primary_cost = float(declaration["primary"]["cost_bps"])
    if role_contracts:
        for source, archive in sources.items():
            trades = _read_csv(archive, "tier1_trades.csv")
            state_by_date = {row["date"]: row.get("trend_state") for row in labels[source]}
            for strategy, contract in role_contracts.items():
                if source not in strategy_sources.get(strategy, set()):
                    continue
                if not contract.get("role_overlap_required", True):
                    continue
                holdings = _dominant_holding_by_date(
                    daily_rows[source], trades, strategy, primary_cost
                )
                active_symbols = set(contract["active_symbols"])
                for day, symbol in holdings.items():
                    state = state_by_date.get(day)
                    if not state:
                        continue
                    counts = role_counts[(strategy, state)]
                    counts[0] += 1
                    counts[1] += int(symbol in active_symbols)

    whole_path_rows: list[dict] = []
    for strategy in strategies:
        candidate_returns: list[float] = []
        benchmark_returns: list[float] = []
        used_dates: set[str] = set()
        for block in declaration["fixed_calendar_blocks"]:
            source = block["source"]
            if source not in strategy_sources[strategy]:
                continue
            candidate = paths[source][(strategy, primary_cost)]
            control = paths[source][(benchmark, primary_cost)]
            dates = sorted(
                day for day in set(candidate) & set(control)
                if block["start"] <= day <= block["end"]
            )
            duplicate = used_dates.intersection(dates)
            if duplicate:
                raise ValueError(
                    f"overlapping whole-path dates for {strategy}: {sorted(duplicate)[:3]}"
                )
            used_dates.update(dates)
            candidate_returns.extend(candidate[day] for day in dates)
            benchmark_returns.extend(control[day] for day in dates)
        candidate_metrics = _path_metrics(candidate_returns)
        benchmark_metrics = _path_metrics(benchmark_returns)
        whole_path_rows.append({
            "strategy": strategy,
            **{f"strategy_{key}": value for key, value in candidate_metrics.items()},
            **{f"spy_{key}": value for key, value in benchmark_metrics.items()},
            "relative_wealth_vs_spy": (
                (1.0 + candidate_metrics["total_return"])
                / (1.0 + benchmark_metrics["total_return"]) - 1.0
            ),
        })
    _write_csv(output / "full_roster_whole_path_scorecard.csv", whole_path_rows)

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
                if episode[0] == label_boundaries[source][0] or episode[-1] == label_boundaries[source][1]:
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
                    if source not in strategy_sources[strategy]:
                        continue
                    for cost in costs:
                        candidate = paths[source][(strategy, cost)]
                        control = paths[source][(benchmark, cost)]
                        if any(day not in candidate or day not in control for day in episode):
                            if strategy == strategies[0] and cost == costs[0]:
                                excluded_rows.append({
                                    "block": block_id, "source": source,
                                    "trend_state": state,
                                    "source_episode_number": episode_number,
                                    "episode_start": episode[0], "episode_end": episode[-1],
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
                                "strategy": strategy, "trend_state": state,
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
    _write_csv(output / "full_roster_state_episode_scorecard.csv", episode_rows)
    _write_csv(output / "full_roster_state_excluded_episodes.csv", excluded_rows)

    minimum_block_episodes = int(declaration["eligible_block"]["minimum_complete_episodes"])
    minimum_block_sessions = int(declaration["eligible_block"]["minimum_scored_sessions"])
    block_rows: list[dict] = []
    for strategy in strategies:
        for state in states:
            for cost in costs:
                for lag in lags:
                    relevant = [
                        row for row in episode_rows
                        if row["strategy"] == strategy and row["trend_state"] == state
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
                            "strategy": strategy, "trend_state": state,
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
    _write_csv(output / "full_roster_state_block_scorecard.csv", block_rows)

    aggregate_rows: list[dict] = []
    for strategy in strategies:
        for state in states:
            for cost in costs:
                for lag in lags:
                    rows = [
                        row for row in episode_rows
                        if row["strategy"] == strategy and row["trend_state"] == state
                        and row["cost_bps"] == cost
                        and row["activation_lag_sessions"] == lag
                    ]
                    eligible = [
                        row for row in block_rows
                        if row["strategy"] == strategy and row["trend_state"] == state
                        and row["cost_bps"] == cost
                        and row["activation_lag_sessions"] == lag
                        and row["eligible_block"]
                    ]
                    metrics = _episode_metrics(rows)
                    positives = sum(bool(row["positive_block"]) for row in eligible)
                    aggregate_rows.append({
                        "strategy": strategy, "trend_state": state,
                        "cost_bps": cost, "activation_lag_sessions": lag,
                        **metrics,
                        "eligible_blocks": len(eligible),
                        "positive_blocks": positives,
                        "positive_block_fraction": positives / len(eligible) if eligible else None,
                        "one_sided_episode_sign_p": _binomial_sign_p(
                            metrics["wins"], metrics["episodes"]
                        ),
                    })
    _write_csv(output / "full_roster_state_aggregate_scorecard.csv", aggregate_rows)

    primary_lag = int(declaration["primary"]["activation_lag_sessions"])
    stress_cost = float(declaration["stress"]["cost_bps"])
    stress_lag = int(declaration["stress"]["activation_lag_sessions"])
    primary_rows = [
        row for row in aggregate_rows
        if row["cost_bps"] == primary_cost
        and row["activation_lag_sessions"] == primary_lag
    ]
    if len(primary_rows) != declaration["family_size"]:
        raise ValueError("primary inference family is incomplete")
    gates = declaration["reliability_gate"]
    inference_p_values = [
        float(row["one_sided_episode_sign_p"])
        if row["episodes"] >= gates["minimum_complete_episodes"] else 1.0
        for row in primary_rows
    ]
    q_values = _bh_adjust(inference_p_values)
    acceptance_rows: list[dict] = []
    for primary, inference_p, q_value in zip(primary_rows, inference_p_values, q_values):
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
            "primary_win_rate_pass": (
                primary["episode_win_rate"] is not None
                and primary["episode_win_rate"] >= gates["minimum_primary_episode_win_rate"]
            ),
            "primary_median_pass": (
                primary["median_episode_excess"] is not None
                and primary["median_episode_excess"] > 0.0
                and primary["median_episode_excess"] >= gates.get(
                    "minimum_primary_median_episode_excess", 0.0
                )
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
                and stress["median_episode_excess"] >= gates.get(
                    "minimum_stress_median_episode_excess", 0.0
                )
            ),
        }
        if "minimum_stress_profit_factor" in gates:
            checks["stress_profit_factor_pass"] = (
                stress["profit_factor"] is not None
                and stress["profit_factor"] >= gates["minimum_stress_profit_factor"]
            )
        contract = role_contracts.get(primary["strategy"])
        role_total, role_active = role_counts[(
            primary["strategy"], primary["trend_state"]
        )]
        role_fraction = role_active / role_total if role_total else None
        role_required = bool(contract and contract.get("role_overlap_required", True))
        checks["active_holding_fraction_pass"] = (
            not role_required
            or (
                role_fraction is not None
                and role_fraction >= gates["minimum_active_holding_fraction"]
            )
        )
        acceptance_rows.append({
            "strategy": primary["strategy"], "trend_state": primary["trend_state"],
            "primary_episodes": primary["episodes"],
            "primary_eligible_blocks": primary["eligible_blocks"],
            "primary_positive_blocks": primary["positive_blocks"],
            "primary_positive_block_fraction": primary["positive_block_fraction"],
            "primary_episode_win_rate": primary["episode_win_rate"],
            "primary_median_episode_excess": primary["median_episode_excess"],
            "primary_mean_episode_excess": primary["mean_episode_excess"],
            "primary_profit_factor": primary["profit_factor"],
            "primary_payoff_ratio": primary["payoff_ratio"],
            "primary_worst_episode_excess": primary["worst_episode_excess"],
            "primary_sign_p": primary["one_sided_episode_sign_p"],
            "primary_family_inference_p": inference_p,
            "primary_bh_q": q_value,
            "stress_episode_win_rate": stress["episode_win_rate"],
            "stress_median_episode_excess": stress["median_episode_excess"],
            "stress_profit_factor": stress["profit_factor"],
            "role_overlap_required": role_required,
            "role_state_sessions": role_total,
            "role_active_sessions": role_active,
            "active_holding_fraction": role_fraction,
            **checks,
            "criteria_passed": sum(checks.values()),
            "criteria_total": len(checks),
            "reliability_gate_pass": all(checks.values()),
        })
    _write_csv(output / "full_roster_state_acceptance.csv", acceptance_rows)

    ranked = sorted(
        acceptance_rows,
        key=lambda row: (
            -int(row["criteria_passed"]), float(row["primary_bh_q"]),
            -float(row["primary_episode_win_rate"] or 0.0),
            row["strategy"], row["trend_state"],
        ),
    )
    summary = {
        "study_id": declaration["study_id"],
        "family_size": declaration["family_size"],
        "strategies": len(strategies), "trend_states": len(states),
        "complete_episode_rows": len(episode_rows),
        "excluded_episode_records": len(excluded_rows),
        "passed": [
            {"strategy": row["strategy"], "trend_state": row["trend_state"]}
            for row in acceptance_rows if row["reliability_gate_pass"]
        ],
        "closest_cells_descriptive_only": [
            {
                "strategy": row["strategy"], "trend_state": row["trend_state"],
                "criteria_passed": row["criteria_passed"],
                "criteria_total": row["criteria_total"],
                "primary_bh_q": row["primary_bh_q"],
            }
            for row in ranked[:10]
        ],
        "source_sha256": {source: _sha256(path) for source, path in sources.items()},
        "declaration_sha256": _sha256(declaration_path),
        "generation_015_effect": "none", "router_effect": "none",
        "paper_trading_effect": "none", "live_trading_effect": "none",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "full_roster_state_summary.json").write_text(
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
        default=Path(__file__).with_name("full-roster-state-reliability-study-001.json"),
    )
    parser.add_argument("--prehistory", type=Path)
    parser.add_argument("--modern", type=Path)
    parser.add_argument(
        "--archive", action="append", default=[], metavar="NAME=PATH",
        help="Named immutable archive; repeat for multi-archive studies",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    archives = None
    if args.archive:
        archives = {}
        for value in args.archive:
            name, separator, path = value.partition("=")
            if not separator or not name or not path:
                parser.error("--archive must use NAME=PATH")
            if name in archives:
                parser.error(f"duplicate archive name: {name}")
            archives[name] = Path(path)
    print(json.dumps(build(
        args.declaration, args.prehistory, args.modern, args.output,
        archives=archives,
    ), indent=2))


if __name__ == "__main__":
    main()
