from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load(path: str | Path) -> list[dict]:
    with Path(path).open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def compare_replay_to_live(replay_rows: list[dict], live_rows: list[dict]) -> dict:
    """Compare replay cycles with exported research-shadow cycles by source bar."""
    replay_index = {
        (str(row.get("timestamp")), str(row.get("strategy_name"))): row
        for row in replay_rows
    }
    live_index = {
        (str(row.get("source_bar_timestamp")), str(row.get("strategy_name"))): row
        for row in live_rows
        if row.get("source_bar_timestamp")
    }
    common = sorted(set(replay_index) & set(live_index))
    metrics = {
        "equity": ("equity", "shadow_equity"),
        "turnover": ("turnover", "cumulative_gross_turnover"),
        "trade_count": ("trade_count", "cumulative_trade_count"),
        "drawdown_pct": ("drawdown_pct", "drawdown_pct"),
    }
    by_strategy = defaultdict(lambda: defaultdict(list))
    examples = []
    for key in common:
        replay, live = replay_index[key], live_index[key]
        strategy = key[1]
        example = {"source_bar_timestamp": key[0], "strategy_name": strategy}
        for metric, (replay_field, live_field) in metrics.items():
            left, right = _number(replay.get(replay_field)), _number(live.get(live_field))
            if left is None or right is None:
                continue
            difference = left - right
            by_strategy[strategy][metric].append(difference)
            example[f"{metric}_replay_minus_live"] = round(difference, 8)
        if len(examples) < 25:
            examples.append(example)
    summaries = []
    for strategy, strategy_metrics in sorted(by_strategy.items()):
        row = {"strategy_name": strategy, "matched_cycles": sum(1 for key in common if key[1] == strategy)}
        for metric, differences in strategy_metrics.items():
            row[f"{metric}_mean_absolute_error"] = round(sum(abs(value) for value in differences) / len(differences), 8)
            row[f"{metric}_max_absolute_error"] = round(max(abs(value) for value in differences), 8)
            row[f"{metric}_final_difference"] = round(differences[-1], 8)
        summaries.append(row)
    replay_only = sorted(set(replay_index) - set(live_index))
    live_only = sorted(set(live_index) - set(replay_index))
    return {
        "matched_cycle_count": len(common),
        "replay_only_cycle_count": len(replay_only),
        "live_only_cycle_count": len(live_only),
        "strategy_summaries": summaries,
        "difference_examples": examples,
        "status": "ok" if common else "no_matching_source_cycles",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare historical replay cycles with exported live research-shadow cycles.")
    parser.add_argument("replay_cycles_csv")
    parser.add_argument("live_research_cycles_csv")
    parser.add_argument("--output", default="replay_output/replay_parity.json")
    args = parser.parse_args(argv)
    result = compare_replay_to_live(_load(args.replay_cycles_csv), _load(args.live_research_cycles_csv))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
