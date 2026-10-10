"""Frozen Phase 014 validation for an unlevered TIPP equity baseline."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
import zipfile
from pathlib import Path


ERAS = (
    ("OLDER_2008_2010", "prehistory", "2008-05-30", "2010-12-31"),
    ("MODERN_2012_2014", "modern", "2012-01-01", "2014-12-31"),
    ("MODERN_2015_2017", "modern", "2015-01-01", "2017-12-31"),
    ("MODERN_2018_2020", "modern", "2018-01-01", "2020-12-31"),
    ("MODERN_2021_2023", "modern", "2021-01-01", "2023-12-31"),
    ("MODERN_2024_CURRENT", "modern", "2024-01-01", "9999-12-31"),
)
COSTS = (10.0, 20.0)
FLOOR_FRACTION = 0.80
MULTIPLIER = 5.0
NO_TRADE_BAND = 0.02


def _rows(archive: Path) -> list[dict]:
    with zipfile.ZipFile(archive) as bundle:
        return list(csv.DictReader(io.StringIO(
            bundle.read("tier1_daily.csv").decode("utf-8-sig")
        )))


def _spy_returns(rows: list[dict], cost: float) -> list[tuple[str, float]]:
    selected = sorted(
        (row for row in rows if row["strategy"] == "SPY_BUY_HOLD" and
         float(row["cost_bps"]) == cost), key=lambda row: row["date"]
    )
    previous = 100000.0
    output = []
    for row in selected:
        equity = float(row["equity"])
        output.append((row["date"], equity / previous - 1.0))
        previous = equity
    return output


def simulate_tipp(spy_returns: list[tuple[str, float]], cost_bps: float) -> list[dict]:
    value = high_watermark = 100000.0
    floor = FLOOR_FRACTION * high_watermark
    equity_weight = min(1.0, MULTIPLIER * (value - floor) / value)
    output = []
    for day, spy_return in spy_returns:
        opening_value = value
        value *= 1.0 + equity_weight * spy_return
        high_watermark = max(high_watermark, value)
        floor = FLOOR_FRACTION * high_watermark
        target = max(0.0, min(1.0, MULTIPLIER * max(0.0, value - floor) / value))
        turnover = abs(target - equity_weight)
        if turnover < NO_TRADE_BAND:
            target = equity_weight
            turnover = 0.0
        cost = value * turnover * cost_bps / 10000.0
        value -= cost
        floor_breach = value < floor
        output.append({
            "date": day,
            "equity": value,
            "daily_return": value / opening_value - 1.0,
            "equity_weight": target,
            "cash_weight": 1.0 - target,
            "floor": floor,
            "high_watermark": high_watermark,
            "turnover": turnover,
            "modeled_cost": cost,
            "floor_breach": floor_breach,
        })
        equity_weight = target
    return output


def _metrics(returns: list[float]) -> dict:
    wealth = peak = 1.0
    drawdown = 0.0
    for value in returns:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1.0)
    volatility = statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    annual_return = statistics.fmean(returns) * 252 if returns else 0.0
    return {
        "sessions": len(returns),
        "total_return": wealth - 1.0,
        "cagr": wealth ** (252 / len(returns)) - 1.0 if returns else None,
        "annualized_volatility": volatility,
        "sharpe": annual_return / volatility if volatility else 0.0,
        "max_drawdown": drawdown,
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build_validation(prehistory: Path, modern: Path, output: Path) -> dict:
    sources = {"prehistory": _rows(prehistory), "modern": _rows(modern)}
    path_rows = []
    score_rows = []
    for cost in COSTS:
        simulations = {
            source: simulate_tipp(_spy_returns(rows, cost), cost)
            for source, rows in sources.items()
        }
        spy_paths = {
            source: dict(_spy_returns(rows, cost)) for source, rows in sources.items()
        }
        for source, values in simulations.items():
            for row in values:
                path_rows.append({"source": source, "cost_bps": cost, **row})
        for era, source, start, end in ERAS:
            strategy = [row for row in simulations[source] if start <= row["date"] <= end]
            spy = [value for day, value in spy_paths[source].items() if start <= day <= end]
            if not strategy or not spy:
                continue
            candidate_metrics = _metrics([row["daily_return"] for row in strategy])
            spy_metrics = _metrics(spy)
            relative = ((1 + candidate_metrics["total_return"]) /
                        (1 + spy_metrics["total_return"]) - 1)
            score_rows.append({
                "era": era,
                "source": source,
                "start": strategy[0]["date"],
                "end": strategy[-1]["date"],
                "cost_bps": cost,
                **candidate_metrics,
                "spy_total_return": spy_metrics["total_return"],
                "spy_cagr": spy_metrics["cagr"],
                "spy_sharpe": spy_metrics["sharpe"],
                "spy_max_drawdown": spy_metrics["max_drawdown"],
                "relative_wealth_vs_spy": relative,
                "beat_spy": relative > 0,
                "lower_drawdown_than_spy": candidate_metrics["max_drawdown"] > spy_metrics["max_drawdown"],
                "higher_sharpe_than_spy": candidate_metrics["sharpe"] > spy_metrics["sharpe"],
                "spy_cagr_retention": (candidate_metrics["cagr"] / spy_metrics["cagr"]
                                       if spy_metrics["cagr"] and spy_metrics["cagr"] > 0 else None),
                "floor_breaches": sum(row["floor_breach"] for row in strategy),
                "one_way_turnover": sum(row["turnover"] for row in strategy),
                "ending_equity_weight": strategy[-1]["equity_weight"],
            })
    _write(output / "phase014_daily_path.csv", path_rows)
    _write(output / "phase014_era_scorecard.csv", score_rows)

    acceptance = []
    for cost in COSTS:
        selected = [row for row in score_rows if row["cost_bps"] == cost]
        retention = [row["spy_cagr_retention"] for row in selected
                     if row["spy_cagr_retention"] is not None]
        row = {
            "cost_bps": cost,
            "blocks": len(selected),
            "blocks_lower_drawdown": sum(row["lower_drawdown_than_spy"] for row in selected),
            "blocks_higher_sharpe": sum(row["higher_sharpe_than_spy"] for row in selected),
            "blocks_beating_spy": sum(row["beat_spy"] for row in selected),
            "median_spy_cagr_retention": statistics.median(retention) if retention else None,
            "floor_breaches": sum(int(row["floor_breaches"]) for row in selected),
        }
        row["acceptance_pass"] = (
            row["blocks"] == 6 and row["blocks_lower_drawdown"] >= 5 and
            row["blocks_higher_sharpe"] >= 4 and row["blocks_beating_spy"] >= 2 and
            row["median_spy_cagr_retention"] >= 0.85 and row["floor_breaches"] == 0
        )
        acceptance.append(row)
    _write(output / "phase014_acceptance.csv", acceptance)
    summary = {
        "study_id": "LONG_TERM_PORTFOLIO_INSURANCE_PHASE_014",
        "strategy": "TIPP_EQUITY_INSURANCE",
        "passed": all(row["acceptance_pass"] for row in acceptance),
        "cost_verdicts": {str(row["cost_bps"]): row["acceptance_pass"] for row in acceptance},
        "router_effect": "none",
        "paper_trading_effect": "none",
        "generation_015_effect": "none",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "phase014_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
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
