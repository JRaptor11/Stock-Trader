"""Validate the frozen SPY/VIG/PKW fully invested equity baseline."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


SYMBOLS = ("SPY", "VIG", "PKW")
TARGET = {symbol: 1.0 / 3.0 for symbol in SYMBOLS}
COSTS = (10.0, 20.0)
NO_TRADE_BAND = 0.02
ERAS = (
    ("OLDER_2008_2010", "2008-01-01", "2010-12-31"),
    ("MODERN_2012_2014", "2012-01-01", "2014-12-31"),
    ("MODERN_2015_2017", "2015-01-01", "2017-12-31"),
    ("MODERN_2018_2020", "2018-01-01", "2020-12-31"),
    ("MODERN_2021_2023", "2021-01-01", "2023-12-31"),
    ("MODERN_2024_CURRENT", "2024-01-01", "9999-12-31"),
)


def _prices(path: Path) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = defaultdict(dict)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            symbol = row["symbol"]
            if symbol in SYMBOLS:
                output[symbol][row["timestamp"][:10]] = float(row["close"])
    return output


def _asset_returns(prices: dict[str, dict[str, float]]) -> tuple[list[str], dict[str, dict[str, float]]]:
    dates = sorted(set.intersection(*(set(prices[symbol]) for symbol in SYMBOLS)))
    returns = {symbol: {} for symbol in SYMBOLS}
    for symbol in SYMBOLS:
        for index in range(1, len(dates)):
            returns[symbol][dates[index]] = prices[symbol][dates[index]] / prices[symbol][dates[index - 1]] - 1.0
    return dates[1:], returns


def _is_quarterly_rebalance(day: str, previous_day: str | None) -> bool:
    year, month = map(int, day.split("-")[:2])
    if month not in (1, 4, 7, 10):
        return False
    if previous_day is None:
        return True
    prior_year, prior_month = map(int, previous_day.split("-")[:2])
    return (year, month) != (prior_year, prior_month)


def simulate(prices: dict[str, dict[str, float]], cost_bps: float) -> tuple[list[dict], list[dict]]:
    dates, returns = _asset_returns(prices)
    value = 100000.0 * (1.0 - cost_bps / 10000.0)
    spy_value = 100000.0 * (1.0 - cost_bps / 10000.0)
    weights = dict(TARGET)
    candidate, benchmark = [], []
    previous_day = None
    for day in dates:
        opening = value
        gross = {symbol: weights[symbol] * (1.0 + returns[symbol][day]) for symbol in SYMBOLS}
        total = sum(gross.values())
        value *= total
        weights = {symbol: gross[symbol] / total for symbol in SYMBOLS}
        turnover = 0.0
        if _is_quarterly_rebalance(day, previous_day):
            changes = {symbol: TARGET[symbol] - weights[symbol] for symbol in SYMBOLS}
            if max(abs(change) for change in changes.values()) >= NO_TRADE_BAND:
                turnover = sum(abs(change) for change in changes.values()) / 2.0
                value *= 1.0 - turnover * cost_bps / 10000.0
                weights = dict(TARGET)
        candidate.append({
            "date": day, "equity": value, "daily_return": value / opening - 1.0,
            "one_way_turnover": turnover, **{f"{s.lower()}_weight": weights[s] for s in SYMBOLS},
        })
        spy_opening = spy_value
        spy_value *= 1.0 + returns["SPY"][day]
        benchmark.append({"date": day, "equity": spy_value,
                          "daily_return": spy_value / spy_opening - 1.0})
        previous_day = day
    return candidate, benchmark


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
        "sessions": len(returns), "total_return": wealth - 1.0,
        "cagr": wealth ** (252 / len(returns)) - 1.0 if returns else None,
        "annualized_volatility": volatility,
        "sharpe": annual_return / volatility if volatility else 0.0,
        "max_drawdown": drawdown,
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def build_validation(data: Path, output: Path) -> dict:
    prices = _prices(data)
    common_dates, asset_returns = _asset_returns(prices)
    coverage = {symbol: {"start": min(values), "end": max(values), "sessions": len(values)}
                for symbol, values in prices.items()}
    (output).mkdir(parents=True, exist_ok=True)
    (output / "phase001_coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    score_rows, daily_rows = [], []
    for cost in COSTS:
        strategy, spy = simulate(prices, cost)
        daily_rows.extend({"cost_bps": cost, **row} for row in strategy)
        spy_by_day = {row["date"]: row for row in spy}
        for era, start, end in ERAS:
            selected = [row for row in strategy if start <= row["date"] <= end]
            benchmark = [spy_by_day[row["date"]] for row in selected]
            candidate_metrics = _metrics([row["daily_return"] for row in selected])
            spy_metrics = _metrics([row["daily_return"] for row in benchmark])
            relative = ((1 + candidate_metrics["total_return"]) /
                        (1 + spy_metrics["total_return"]) - 1)
            score_rows.append({
                "era": era, "start": selected[0]["date"], "end": selected[-1]["date"],
                "cost_bps": cost, **candidate_metrics,
                "spy_total_return": spy_metrics["total_return"], "spy_cagr": spy_metrics["cagr"],
                "spy_sharpe": spy_metrics["sharpe"], "spy_max_drawdown": spy_metrics["max_drawdown"],
                "relative_wealth_vs_spy": relative, "beat_spy": relative > 0,
                "lower_drawdown_than_spy": candidate_metrics["max_drawdown"] > spy_metrics["max_drawdown"],
                "higher_sharpe_than_spy": candidate_metrics["sharpe"] > spy_metrics["sharpe"],
                "spy_cagr_retention": (candidate_metrics["cagr"] / spy_metrics["cagr"]
                                       if spy_metrics["cagr"] and spy_metrics["cagr"] > 0 else None),
                "one_way_turnover": sum(row["one_way_turnover"] for row in selected),
            })
    _write(output / "phase001_daily_path.csv", daily_rows)
    _write(output / "phase001_era_scorecard.csv", score_rows)

    # Attribution is diagnostic only: it explains the frozen blend and cannot
    # be used to select a new weight or drop a sleeve on this same sample.
    sleeve_rows = []
    for era, start, end in ERAS:
        for symbol in SYMBOLS:
            selected = [asset_returns[symbol][day] for day in common_dates
                        if start <= day <= end]
            spy = [asset_returns["SPY"][day] for day in common_dates
                   if start <= day <= end]
            metrics = _metrics(selected)
            spy_metrics = _metrics(spy)
            sleeve_rows.append({
                "era": era, "symbol": symbol, **metrics,
                "spy_total_return": spy_metrics["total_return"],
                "relative_wealth_vs_spy": ((1 + metrics["total_return"]) /
                                           (1 + spy_metrics["total_return"]) - 1),
                "diagnostic_only": True,
            })
    _write(output / "phase001_sleeve_attribution.csv", sleeve_rows)
    acceptance = []
    for cost in COSTS:
        selected = [row for row in score_rows if row["cost_bps"] == cost]
        relative = [row["relative_wealth_vs_spy"] for row in selected]
        retention = [row["spy_cagr_retention"] for row in selected if row["spy_cagr_retention"] is not None]
        row = {
            "cost_bps": cost, "blocks": len(selected),
            "blocks_beating_spy": sum(row["beat_spy"] for row in selected),
            "median_relative_wealth": statistics.median(relative),
            "worst_relative_wealth": min(relative),
            "blocks_lower_drawdown": sum(row["lower_drawdown_than_spy"] for row in selected),
            "blocks_higher_sharpe": sum(row["higher_sharpe_than_spy"] for row in selected),
            "median_spy_cagr_retention": statistics.median(retention),
        }
        row["acceptance_pass"] = (
            row["blocks"] == 6 and row["blocks_beating_spy"] >= 3 and
            row["median_relative_wealth"] > 0 and row["worst_relative_wealth"] >= -0.10 and
            row["blocks_lower_drawdown"] >= 4 and row["blocks_higher_sharpe"] >= 4 and
            row["median_spy_cagr_retention"] >= 0.95
        )
        acceptance.append(row)
    _write(output / "phase001_acceptance.csv", acceptance)
    summary = {
        "study_id": "FULLY_INVESTED_EQUITY_BASELINE_STUDY_001",
        "passed": all(row["acceptance_pass"] for row in acceptance),
        "cost_verdicts": {str(row["cost_bps"]): row["acceptance_pass"] for row in acceptance},
        "router_effect": "none", "paper_trading_effect": "none", "generation_015_effect": "none",
    }
    (output / "phase001_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_validation(args.data, args.output), indent=2))


if __name__ == "__main__":
    main()
