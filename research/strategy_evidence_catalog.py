"""Build a comparable cross-family strategy and market-condition evidence bundle."""

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

from research.strategy_registry import HYPOTHESES


DAILY_MEMBERS = ("tier1_daily.csv", "intraday_daily.csv")
BENCHMARK_MEMBERS = ("intraday_benchmark_daily.csv",)
CONDITION_MEMBERS = ("tier1_market_conditions.csv", "intraday_market_conditions.csv")
MANIFEST_MEMBERS = ("tier1_manifest.json", "intraday_aggregate_manifest.json", "intraday_manifest.json")


def _rows(bundle: zipfile.ZipFile, name: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8-sig"))))


def _first_member(bundle: zipfile.ZipFile, candidates: tuple[str, ...]) -> str | None:
    names = set(bundle.namelist())
    return next((name for name in candidates if name in names), None)


def _family(path: Path, manifest: dict, strategies: set[str]) -> tuple[str, str, str, str]:
    experiment = manifest.get("experiment") or {}
    declared = str(experiment.get("hypothesis_id") or manifest.get("hypothesis_id") or "UNDECLARED")
    identity = manifest.get("strategy_identity") or []
    universe = str((manifest.get("config") or {}).get("universe_name") or "")
    inferred = str(identity[0]) if identity and isinstance(identity, list) else declared
    if universe == "ETF_TIER2_MULTI_SLEEVE" or {"FACTOR_ETF_MOMENTUM", "INDUSTRY_ETF_MOMENTUM", "STATIC_MULTI_SLEEVE"}.intersection(strategies):
        inferred = "TIER2_ETF_TOURNAMENT"
    warning = "" if declared in ("UNDECLARED", inferred) else f"declared {declared}; inferred {inferred} from strategy/universe identity"
    trial = str(experiment.get("trial_id") or manifest.get("engine") or path.stem)
    return inferred, trial, declared, warning


def _merge_conditions(target: dict[str, dict], rows: list[dict]) -> None:
    for row in rows:
        day = row["date"]
        existing = target.get(day)
        if existing and any(existing.get(key) != value for key, value in row.items() if key.endswith("_bucket")):
            raise ValueError(f"conflicting causal condition labels for {day}")
        target[day] = row


def load_condition_archives(paths: list[Path]) -> dict[str, dict]:
    conditions: dict[str, dict] = {}
    for path in paths:
        with zipfile.ZipFile(path) as bundle:
            member = _first_member(bundle, CONDITION_MEMBERS)
            if not member:
                raise ValueError(f"{path.name}: no supported causal market-condition member")
            _merge_conditions(conditions, _rows(bundle, member))
    return conditions


def load_archives(paths: list[Path], cost_bps: float, require_conditions: bool = True) -> tuple[list[dict], dict[str, dict], list[dict]]:
    observations: list[dict] = []
    conditions: dict[str, dict] = {}
    sources: list[dict] = []
    for path in paths:
        with zipfile.ZipFile(path) as bundle:
            daily_member = _first_member(bundle, DAILY_MEMBERS)
            if not daily_member:
                raise ValueError(f"{path.name}: no supported daily return member")
            manifest_member = _first_member(bundle, MANIFEST_MEMBERS)
            manifest = json.loads(bundle.read(manifest_member)) if manifest_member else {}
            daily = [row for row in _rows(bundle, daily_member) if float(row["cost_bps"]) == float(cost_bps)]
            benchmark_member = _first_member(bundle, BENCHMARK_MEMBERS)
            if benchmark_member and not any(row["strategy"] == "SPY_BUY_HOLD" for row in daily):
                daily.extend(row for row in _rows(bundle, benchmark_member) if float(row["cost_bps"]) == float(cost_bps))
            if not daily:
                raise ValueError(f"{path.name}: no daily rows at {cost_bps:g} bps")
            by_strategy: dict[str, list[dict]] = defaultdict(list)
            for row in daily:
                by_strategy[row["strategy"]].append(row)
            hypothesis, trial, declared_hypothesis, provenance_warning = _family(path, manifest, set(by_strategy))
            for strategy, rows in by_strategy.items():
                prior = None
                for row in sorted(rows, key=lambda item: item["date"]):
                    equity = float(row["equity"])
                    if prior not in (None, 0.0):
                        observations.append({
                            "source": path.name, "hypothesis_id": hypothesis, "trial_id": trial,
                            "strategy": strategy, "date": row["date"], "cost_bps": cost_bps,
                            "daily_return": equity / prior - 1.0,
                        })
                    prior = equity
            condition_member = _first_member(bundle, CONDITION_MEMBERS)
            if condition_member:
                _merge_conditions(conditions, _rows(bundle, condition_member))
            sources.append({
                "source": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "hypothesis_id": hypothesis, "declared_hypothesis_id": declared_hypothesis,
                "provenance_warning": provenance_warning, "trial_id": trial, "daily_member": daily_member,
                "benchmark_member": benchmark_member or "",
                "condition_member": condition_member or "", "strategies": sorted(by_strategy),
            })
    if require_conditions and not conditions:
        raise ValueError("at least one archive must contain causal market conditions")
    return observations, conditions, sources


def _compound(values: list[float]) -> float:
    return math.prod(1.0 + value for value in values) - 1.0


def _maximum_drawdown(values: list[float]) -> float:
    equity = peak = 1.0
    drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    return drawdown


def build_catalog(observations: list[dict], conditions: dict[str, dict], cost_bps: float,
                  minimum_samples: int = 30) -> tuple[list[dict], list[dict], dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in observations:
        grouped[(row["source"], row["strategy"])].append(row)
    common_dates = set.intersection(*(set(row["date"] for row in rows) for rows in grouped.values())) if grouped else set()
    if not common_dates:
        raise ValueError("strategy archives have no common return dates")
    condition_dates = common_dates.intersection(conditions)
    bucket_dimensions = sorted({key for day in condition_dates for key in conditions[day] if key.endswith("_bucket")})

    catalog = []
    normalized: dict[tuple[str, str], dict[str, float]] = {}
    for key, rows in sorted(grouped.items()):
        selected = {row["date"]: float(row["daily_return"]) for row in rows if row["date"] in common_dates}
        normalized[key] = selected
        sample = next(row for row in rows)
        registry = HYPOTHESES.get(sample["strategy"], HYPOTHESES.get(sample["hypothesis_id"], {}))
        catalog.append({
            "source": key[0], "hypothesis_id": sample["hypothesis_id"], "trial_id": sample["trial_id"],
            "strategy": key[1], "data_frequency": registry.get("data_frequency", "unknown"),
            "mechanism": registry.get("mechanism", "recorded by source experiment"),
            "cost_bps": cost_bps, "comparison_start": min(common_dates), "comparison_end": max(common_dates),
            "sessions": len(selected), "compounded_return": _compound(list(selected.values())),
        })

    benchmark_keys = [key for key in normalized if key[1] == "SPY_BUY_HOLD"]
    if not benchmark_keys:
        raise ValueError("SPY_BUY_HOLD is required for matched condition comparisons")
    benchmark = normalized[benchmark_keys[0]]
    for key in benchmark_keys[1:]:
        if any(abs(normalized[key][day] - benchmark[day]) > 1e-10 for day in common_dates):
            raise ValueError("SPY_BUY_HOLD returns conflict across source archives")
    matrix = []
    for dimension in bucket_dimensions:
        buckets = sorted({conditions[day].get(dimension) for day in condition_dates if conditions[day].get(dimension)})
        for bucket in buckets:
            dates = sorted(day for day in condition_dates if conditions[day].get(dimension) == bucket)
            benchmark_return = _compound([benchmark[day] for day in dates])
            candidates = []
            for key, returns in normalized.items():
                values = [returns[day] for day in dates]
                candidates.append((key, _compound(values)))
            eligible = sorted((item for item in candidates if len(dates) >= minimum_samples), key=lambda item: item[1], reverse=True)
            ranks = {key: rank for rank, (key, _value) in enumerate(eligible, 1)}
            for key, value in candidates:
                sample = next(row for row in grouped[key])
                values = [normalized[key][day] for day in dates]
                matrix.append({
                    "source": key[0], "hypothesis_id": sample["hypothesis_id"], "trial_id": sample["trial_id"],
                    "strategy": key[1], "dimension": dimension, "bucket": bucket, "cost_bps": cost_bps,
                    "comparison_start": min(dates), "comparison_end": max(dates), "sessions": len(dates),
                    "minimum_samples": minimum_samples, "sample_sufficient": len(dates) >= minimum_samples,
                    "compounded_return": value, "spy_return": benchmark_return,
                    "excess_return_vs_spy": value - benchmark_return,
                    "mean_daily_return": statistics.fmean(values),
                    "median_daily_return": statistics.median(values),
                    "daily_win_rate": sum(item > 0 for item in values) / len(values),
                    "annualized_volatility": statistics.stdev(values) * math.sqrt(252) if len(values) > 1 else None,
                    "conditional_sequence_max_drawdown": _maximum_drawdown(values),
                    "rank_within_comparable_strategies": ranks.get(key, ""),
                })
    metadata = {
        "schema_version": 1, "cost_bps": cost_bps, "minimum_samples": minimum_samples,
        "comparison_start": min(common_dates), "comparison_end": max(common_dates),
        "common_return_sessions": len(common_dates), "condition_labeled_sessions": len(condition_dates),
        "comparability_policy": "all rankings use the intersection of dates shared by every source/strategy",
        "condition_policy": "lagged causal labels supplied by the research market-condition engine",
        "router_policy": "diagnostic evidence only; rankings are descriptive and do not select, combine, or activate strategies",
    }
    return catalog, matrix, metadata


def _csv_bytes(rows: list[dict]) -> bytes:
    output = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    return output.getvalue().encode()


def write_bundle(archives: list[Path], output: Path, cost_bps: float = 10.0,
                 minimum_samples: int = 30, condition_archives: list[Path] | None = None) -> dict:
    observations, conditions, sources = load_archives(archives, cost_bps, require_conditions=not condition_archives)
    if condition_archives:
        _merge_conditions(conditions, list(load_condition_archives(condition_archives).values()))
    catalog, matrix, metadata = build_catalog(observations, conditions, cost_bps, minimum_samples)
    manifest = {**metadata, "sources": sources, "catalog_rows": len(catalog), "condition_matrix_rows": len(matrix)}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("strategy_evidence_catalog.csv", _csv_bytes(catalog))
        bundle.writestr("strategy_condition_matrix.csv", _csv_bytes(matrix))
        bundle.writestr("normalized_daily_returns.csv", _csv_bytes(sorted(observations, key=lambda row: (row["source"], row["strategy"], row["date"]))))
        condition_rows = [conditions[day] for day in sorted(conditions)]
        bundle.writestr("causal_market_conditions.csv", _csv_bytes(condition_rows))
        bundle.writestr("strategy_evidence_manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="append", required=True, type=Path)
    parser.add_argument("--condition-archive", action="append", type=Path, default=[])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--minimum-samples", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(write_bundle(args.archive, args.output, args.cost_bps, args.minimum_samples, args.condition_archive), indent=2))


if __name__ == "__main__":
    main()
