"""Run frozen robustness diagnostics for every Tier 2 finalist."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.tier1_robustness import analyze


FINALISTS = (
    "SECTOR_ETF_ROTATION",
    "FACTOR_ETF_MOMENTUM",
    "INDUSTRY_ETF_MOMENTUM",
    "STATIC_MULTI_SLEEVE",
)


def analyze_finalists(archive: Path, bars: Path, output: Path,
                      cost_bps: float = 10.0) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    runs = []
    for strategy in FINALISTS:
        benchmarks = ["SPY_BUY_HOLD"]
        if strategy != "STATIC_MULTI_SLEEVE":
            benchmarks.append("STATIC_MULTI_SLEEVE")
        for benchmark in benchmarks:
            destination = output / f"{strategy.lower()}-vs-{benchmark.lower()}"
            summary_path = destination / "robustness_summary.json"
            if not summary_path.is_file():
                analyze(archive, bars, destination, strategy, cost_bps, benchmark)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            runs.append({
                "strategy": strategy,
                "benchmark": benchmark,
                "output": destination.name,
                "bootstrap_probability_excess_positive": summary["bootstrap"]["probability_excess_positive"],
                "bootstrap_ci_95": summary["bootstrap"]["excess_return_ci_95"],
                "calendar_year_win_rate": summary["calendar_year_win_rate"],
                "rolling_win_rates": summary["rolling_win_rates"],
                "paper_trading_approved": False,
            })
    (output / "finalist_robustness_manifest.json").write_text(
        json.dumps({
            "source_archive": archive.name,
            "cost_bps": cost_bps,
            "finalists": list(FINALISTS),
            "comparisons": runs,
            "interpretation": "development diagnostics only; no automatic promotion",
        }, indent=2), encoding="utf-8",
    )
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    args = parser.parse_args()
    print(analyze_finalists(args.archive, args.bars, args.output, args.cost_bps))


if __name__ == "__main__":
    main()
