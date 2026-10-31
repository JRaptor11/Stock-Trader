"""Economic and role-relevance audit for complete execution-aware strategies."""

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


ACTIVE_SYMBOL = {
    "QQQ_BULL_TREND_LEADERSHIP": "QQQ",
    "QQQ_BULL_DECELERATION_EXPLORATORY": "QQQ",
    "BIL_RAW_BEAR_DETERIORATION": "BIL",
    "BIL_THREE_DAY_BEAR_DETERIORATION": "BIL",
    "BIL_RAW_TRANSITION_OR_BEAR_DETERIORATION": "BIL",
}


def _archive_rows(path: Path, member: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as bundle:
        with bundle.open(member) as raw:
            return list(csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8")))


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def holding_by_date(daily: list[dict[str, str]], trades: list[dict[str, str]],
                    strategy: str, cost_bps: float) -> dict[str, str]:
    """Reconstruct the dominant holding after each session's opening trades."""
    cost = str(float(cost_bps))
    dates = sorted({row["date"] for row in daily
                    if row["strategy"] == strategy and row["cost_bps"] == cost})
    by_date: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in trades:
        if row["strategy"] == strategy and row["cost_bps"] == cost:
            by_date[row["date"]].append(row)
    current = ""
    result: dict[str, str] = {}
    for day in dates:
        buys = [row for row in by_date.get(day, []) if float(row["notional"]) > 0.0]
        if buys:
            current = max(buys, key=lambda row: float(row["notional"]))["symbol"]
        result[day] = current
    return result


def _path_metrics(returns: list[float]) -> dict[str, float | int]:
    wealth = 1.0
    peak = 1.0
    maximum_drawdown = 0.0
    for value in returns:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        maximum_drawdown = min(maximum_drawdown, wealth / peak - 1.0)
    sessions = len(returns)
    volatility = statistics.pstdev(returns) * math.sqrt(252.0) if returns else 0.0
    cagr = wealth ** (252.0 / sessions) - 1.0 if sessions else 0.0
    sharpe = ((statistics.mean(returns) * 252.0) / volatility
              if volatility > 0.0 else 0.0)
    return {
        "sessions": sessions,
        "total_return": wealth - 1.0,
        "cagr": cagr,
        "annualized_volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": maximum_drawdown,
    }


def audit(*, declaration_path: Path, prehistory_path: Path, modern_path: Path,
          acceptance_path: Path, output_dir: Path) -> dict:
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    acceptance = list(csv.DictReader(acceptance_path.open(encoding="utf-8")))
    primary_cost = float(declaration["primary"]["cost_bps"])
    round_trip_floor = 2.0 * primary_cost / 10000.0
    declared_contracts = declaration.get("strategy_role_contracts")
    if declared_contracts:
        active_symbols = {
            row["strategy"]: set(row["active_symbols"])
            for row in declared_contracts if row.get("role_overlap_required", True)
        }
    else:
        active_symbols = {
            strategy: {symbol} for strategy, symbol in ACTIVE_SYMBOL.items()
        }
    state_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    whole_path: list[dict] = []
    combined_returns: dict[str, list[float]] = defaultdict(list)

    for source, archive in (("prehistory", prehistory_path), ("modern", modern_path)):
        daily = _archive_rows(archive, "tier1_daily.csv")
        trades = _archive_rows(archive, "tier1_trades.csv")
        states = {row["date"]: row for row in _archive_rows(
            archive, "tier1_market_state_labels.csv"
        )}
        scorecards = _archive_rows(archive, "tier1_cost_ladder_scorecard.csv")
        for row in scorecards:
            if float(row["cost_bps"]) not in declaration["costs_bps"]:
                continue
            whole_path.append({"source": source, **row})
        by_strategy: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in daily:
            if float(row["cost_bps"]) == primary_cost:
                by_strategy[row["strategy"]].append(row)
        for strategy, rows in by_strategy.items():
            rows.sort(key=lambda row: row["date"])
            previous = 100000.0
            for row in rows:
                equity = float(row["equity"])
                session_return = equity / previous - 1.0
                previous = equity
                use = ((source == "prehistory" and row["date"] <= "2012-12-31") or
                       (source == "modern" and row["date"] >= "2013-01-01"))
                if use:
                    combined_returns[strategy].append(session_return)
        for strategy, symbols in active_symbols.items():
            holdings = holding_by_date(daily, trades, strategy, primary_cost)
            for day, symbol in holdings.items():
                state = states.get(day, {}).get("trend_state")
                if not state:
                    continue
                counts = state_counts[(strategy, state)]
                counts[0] += 1
                counts[1] += int(symbol in symbols)

    relevance: list[dict] = []
    for row in acceptance:
        strategy = row["strategy"]
        state = row["trend_state"]
        total, active = state_counts[(strategy, state)]
        overlap_required = strategy in active_symbols
        fraction = active / total if total else (0.0 if overlap_required else 1.0)
        median_excess = float(row["primary_median_episode_excess"])
        statistical_pass = row["reliability_gate_pass"] == "True"
        magnitude_pass = median_excess >= round_trip_floor
        overlap_pass = (not overlap_required) or fraction >= 0.50
        relevance.append({
            "strategy": strategy,
            "trend_state": state,
            "confirmed_state_sessions": total,
            "active_holding_sessions": active,
            "active_holding_fraction": fraction,
            "role_overlap_required": overlap_required,
            "primary_median_episode_excess": median_excess,
            "round_trip_materiality_floor": round_trip_floor,
            "statistical_gate_pass": statistical_pass,
            "economic_magnitude_pass": magnitude_pass,
            "role_relevance_pass": overlap_pass,
            "actionable_audit_pass": statistical_pass and magnitude_pass and overlap_pass,
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    combined_path = [
        {"strategy": strategy, "cost_bps": primary_cost,
         "start": "2008-05-30", "end": "2026-09-17",
         **_path_metrics(returns)}
        for strategy, returns in sorted(combined_returns.items())
    ]
    _write_csv(output_dir / "execution_aware_whole_path.csv", whole_path)
    _write_csv(output_dir / "execution_aware_combined_path.csv", combined_path)
    _write_csv(output_dir / "execution_aware_state_relevance.csv", relevance)
    statistical = [r for r in relevance if r["statistical_gate_pass"]]
    actionable = [r for r in relevance if r["actionable_audit_pass"]]
    summary = {
        "study_id": declaration["study_id"],
        "primary_cost_bps": primary_cost,
        "round_trip_materiality_floor": round_trip_floor,
        "statistical_gate_passes": [
            {"strategy": r["strategy"], "trend_state": r["trend_state"]}
            for r in statistical
        ],
        "actionable_audit_passes": [
            {"strategy": r["strategy"], "trend_state": r["trend_state"]}
            for r in actionable
        ],
        "audit_note": (
            "The economic-magnitude and role-overlap checks are post-hoc safeguards "
            "prompted by this run. They invalidate promotion, but cannot create "
            "confirmatory evidence. Future studies must freeze them in advance."
        ),
        "generation_015_effect": "none",
        "router_effect": "none",
        "paper_trading_effect": "none",
        "live_trading_effect": "none",
    }
    (output_dir / "execution_aware_audit_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--prehistory", type=Path, required=True)
    parser.add_argument("--modern", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(
        declaration_path=args.declaration,
        prehistory_path=args.prehistory,
        modern_path=args.modern,
        acceptance_path=args.acceptance,
        output_dir=args.output,
    ), indent=2))


if __name__ == "__main__":
    main()
