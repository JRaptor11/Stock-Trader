"""Chronologically validate strategy-condition relationships without routing capital."""

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

from research.statistical_safeguards import return_evidence


def _rows(bundle: zipfile.ZipFile, name: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8-sig"))))


def _csv_bytes(rows: list[dict]) -> bytes:
    output = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    return output.getvalue().encode()


def _compound(values: list[float]) -> float:
    return math.prod(1.0 + value for value in values) - 1.0


def _load_declaration(path: Path | None, cohort: str | None = None) -> dict | None:
    if path is None:
        return None
    declaration = json.loads(path.read_text(encoding="utf-8"))
    hypotheses = declaration.get("hypotheses") or []
    required = {"id", "strategy", "dimension", "bucket"}
    if not hypotheses or any(not required.issubset(item) for item in hypotheses):
        raise ValueError("focused declaration requires non-empty hypotheses with id, strategy, dimension, and bucket")
    ids = [item["id"] for item in hypotheses]
    if len(ids) != len(set(ids)):
        raise ValueError("focused hypothesis ids must be unique")
    if not declaration.get("frozen_at") or not declaration.get("evidence_observed_through"):
        raise ValueError("focused declaration requires frozen_at and evidence_observed_through")
    if cohort:
        hypotheses = [item for item in hypotheses if item.get("cohort") == cohort]
        if not hypotheses:
            raise ValueError(f"focused declaration has no hypotheses for cohort {cohort}")
        declaration = {**declaration, "hypotheses": hypotheses, "selected_cohort": cohort}
    return declaration


def validate_bundle(path: Path, train_sessions: int = 252, test_sessions: int = 63,
                    step_sessions: int = 63, minimum_bucket_sessions: int = 30,
                    alpha: float = 0.05, declaration_path: Path | None = None,
                    cohort: str | None = None,
                    dimensions: tuple[str, ...] | None = None) -> tuple[list[dict], list[dict], dict]:
    declaration = _load_declaration(declaration_path, cohort)
    with zipfile.ZipFile(path) as bundle:
        names = set(bundle.namelist())
        required = {"normalized_daily_returns.csv", "causal_market_conditions.csv", "strategy_evidence_manifest.json"}
        if not required.issubset(names):
            raise ValueError(f"evidence bundle missing: {sorted(required - names)}")
        daily = _rows(bundle, "normalized_daily_returns.csv")
        conditions = {row["date"]: row for row in _rows(bundle, "causal_market_conditions.csv")}
        source_manifest = json.loads(bundle.read("strategy_evidence_manifest.json"))

    series: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for row in daily:
        series[(row["source"], row["strategy"])][row["date"]] = float(row["daily_return"])
    benchmark_keys = [key for key in series if key[1] == "SPY_BUY_HOLD"]
    if not benchmark_keys:
        raise ValueError("SPY_BUY_HOLD is required")
    benchmark = series[benchmark_keys[0]]
    strategy_keys = [key for key in series if key[1] != "SPY_BUY_HOLD"]
    common_dates = sorted(set(conditions).intersection(benchmark, *(series[key] for key in strategy_keys)))
    if len(common_dates) < train_sessions + test_sessions:
        raise ValueError("insufficient common condition-labeled sessions for one fold")
    available_dimensions = sorted({key for day in common_dates for key in conditions[day] if key.endswith("_bucket")})
    if dimensions:
        missing_requested = sorted(set(dimensions).difference(available_dimensions))
        if missing_requested:
            raise ValueError(f"requested condition dimensions are unavailable: {missing_requested}")
        dimensions = sorted(set(dimensions))
    else:
        dimensions = available_dimensions
    focused = {(item["strategy"], item["dimension"], item["bucket"]): item for item in (declaration or {}).get("hypotheses", [])}
    missing_strategies = sorted({key[0] for key in focused}.difference(key[1] for key in strategy_keys))
    missing_dimensions = sorted({key[1] for key in focused}.difference(dimensions))
    if missing_strategies or missing_dimensions:
        raise ValueError(f"focused declaration is incompatible: missing strategies={missing_strategies}, dimensions={missing_dimensions}")
    family_trials = max(1, len(focused) if focused else len(strategy_keys) * len(dimensions) * 5)

    rows = []
    fold = 0
    for test_start in range(train_sessions, len(common_dates) - test_sessions + 1, step_sessions):
        fold += 1
        train_dates = common_dates[:test_start]
        test_dates = common_dates[test_start:test_start + test_sessions]
        for source, strategy in strategy_keys:
            returns = series[(source, strategy)]
            for dimension in dimensions:
                buckets = sorted({conditions[day].get(dimension) for day in train_dates if conditions[day].get(dimension)})
                for bucket in buckets:
                    hypothesis = focused.get((strategy, dimension, bucket))
                    if focused and not hypothesis:
                        continue
                    train_bucket = [day for day in train_dates if conditions[day].get(dimension) == bucket]
                    test_bucket = [day for day in test_dates if conditions[day].get(dimension) == bucket]
                    train_excess = [returns[day] - benchmark[day] for day in train_bucket]
                    test_excess = [returns[day] - benchmark[day] for day in test_bucket]
                    evidence = return_evidence(train_excess, family_trials) if train_excess else {}
                    adjusted_p = evidence.get("bonferroni_adjusted_p")
                    qualified = (
                        len(train_excess) >= minimum_bucket_sessions
                        and statistics.fmean(train_excess) > 0
                        and adjusted_p is not None and adjusted_p <= alpha
                    )
                    rows.append({
                        "fold": fold, "hypothesis_id": hypothesis["id"] if hypothesis else "", "source": source, "strategy": strategy,
                        "dimension": dimension, "bucket": bucket,
                        "train_start": train_dates[0], "train_end": train_dates[-1],
                        "test_start": test_dates[0], "test_end": test_dates[-1],
                        "train_sessions": len(train_excess), "test_sessions": len(test_excess),
                        "minimum_bucket_sessions": minimum_bucket_sessions,
                        "train_mean_daily_excess": statistics.fmean(train_excess) if train_excess else None,
                        "train_compounded_return": _compound([returns[day] for day in train_bucket]) if train_bucket else None,
                        "train_spy_return": _compound([benchmark[day] for day in train_bucket]) if train_bucket else None,
                        "bonferroni_family_trials": family_trials,
                        "train_adjusted_p": adjusted_p, "qualified_on_train": qualified,
                        "test_compounded_return": _compound([returns[day] for day in test_bucket]) if test_bucket else None,
                        "test_spy_return": _compound([benchmark[day] for day in test_bucket]) if test_bucket else None,
                        "test_excess_return": (_compound([returns[day] for day in test_bucket]) - _compound([benchmark[day] for day in test_bucket])) if test_bucket else None,
                        "test_mean_daily_excess": statistics.fmean(test_excess) if test_excess else None,
                        "test_positive_excess": statistics.fmean(test_excess) > 0 if test_excess else None,
                        "evidence_class": (
                            "exploratory_unfocused" if not declaration else
                            "retrospective_reuse" if test_dates[-1] <= declaration["evidence_observed_through"] else
                            "untouched_forward" if test_dates[0] > declaration["evidence_observed_through"] else
                            "mixed_boundary"
                        ),
                    })

    summary = []
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["source"], row["strategy"], row["dimension"], row["bucket"])].append(row)
    for key, values in sorted(grouped.items()):
        qualified = [row for row in values if row["qualified_on_train"]]
        tested = [row for row in qualified if row["test_mean_daily_excess"] is not None]
        all_tested = [row for row in values if row["test_mean_daily_excess"] is not None]
        summary.append({
            "source": key[0], "strategy": key[1], "dimension": key[2], "bucket": key[3],
            "folds": len(values), "qualified_folds": len(qualified), "qualified_test_folds": len(tested),
            "qualified_test_win_rate": sum(bool(row["test_positive_excess"]) for row in tested) / len(tested) if tested else None,
            "mean_qualified_test_daily_excess": statistics.fmean(float(row["test_mean_daily_excess"]) for row in tested) if tested else None,
            "stable_positive_relationship": bool(tested) and len(tested) >= 2 and sum(bool(row["test_positive_excess"]) for row in tested) / len(tested) >= 0.6,
            "exploratory_all_test_folds": len(all_tested),
            "exploratory_test_win_rate": sum(bool(row["test_positive_excess"]) for row in all_tested) / len(all_tested) if all_tested else None,
            "exploratory_mean_test_daily_excess": statistics.fmean(float(row["test_mean_daily_excess"]) for row in all_tested) if all_tested else None,
            "exploratory_only_not_router_eligible": True,
        })
    metadata = {
        "schema_version": 1, "source_bundle": path.name, "source_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        "source_manifest": source_manifest, "folds": fold, "train_sessions": train_sessions,
        "test_sessions": test_sessions, "step_sessions": step_sessions,
        "minimum_bucket_sessions": minimum_bucket_sessions, "alpha": alpha,
        "condition_dimensions": dimensions,
        "family_trials": family_trials, "selection_policy": "training-only; descriptive validation; no router or execution",
        "focused_declaration": declaration,
        "promotion_policy": "only untouched_forward rows may support promotion; retrospective_reuse and mixed_boundary are development evidence",
        "stable_relationships": sum(bool(row["stable_positive_relationship"]) for row in summary),
    }
    return rows, summary, metadata


def write_validation(source: Path, output: Path, **kwargs) -> dict:
    rows, summary, metadata = validate_bundle(source, **kwargs)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("strategy_condition_walk_forward.csv", _csv_bytes(rows))
        bundle.writestr("strategy_condition_stability.csv", _csv_bytes(summary))
        bundle.writestr("strategy_condition_validation_manifest.json", json.dumps(metadata, indent=2, sort_keys=True))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--train-sessions", type=int, default=252)
    parser.add_argument("--test-sessions", type=int, default=63)
    parser.add_argument("--step-sessions", type=int, default=63)
    parser.add_argument("--minimum-bucket-sessions", type=int, default=30)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--declaration", type=Path)
    parser.add_argument("--cohort")
    parser.add_argument("--dimension", action="append", dest="dimensions",
                        help="Limit the multiplicity family to an explicit condition bucket column; repeatable")
    args = parser.parse_args()
    metadata = write_validation(args.evidence_bundle, args.output, train_sessions=args.train_sessions,
                                test_sessions=args.test_sessions, step_sessions=args.step_sessions,
                                minimum_bucket_sessions=args.minimum_bucket_sessions, alpha=args.alpha,
                                declaration_path=args.declaration, cohort=args.cohort,
                                dimensions=tuple(args.dimensions) if args.dimensions else None)
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
