"""Append causal, frozen strategy-condition observations to a hash-chained ledger."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _hash(value: dict) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _rows(bundle: zipfile.ZipFile, name: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8-sig"))))


def _daily_returns(rows: list[dict], cost_bps: float) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    selected = [row for row in rows if float(row["cost_bps"]) == float(cost_bps)]
    for strategy in sorted({row["strategy"] for row in selected}):
        strategy_rows = sorted((row for row in selected if row["strategy"] == strategy), key=lambda row: row["date"])
        prior = None
        result[strategy] = {}
        for row in strategy_rows:
            equity = float(row["equity"])
            if prior not in (None, 0.0):
                result[strategy][row["date"]] = equity / prior - 1.0
            prior = equity
    return result


def _validate_existing(rows: list[dict]) -> None:
    seen = set()
    previous = None
    for row in rows:
        key = (row.get("hypothesis_id"), row.get("as_of_date"))
        if key in seen:
            raise ValueError(f"duplicate condition-forward observation: {key}")
        if row.get("previous_chain_sha256") != previous:
            raise ValueError("condition-forward ledger chain link is invalid")
        stored = row.get("chain_sha256")
        unsigned = {key: value for key, value in row.items() if key != "chain_sha256"}
        if not stored or _hash(unsigned) != stored:
            raise ValueError("condition-forward ledger row hash is invalid")
        seen.add(key); previous = stored


def append_observations(archive: Path, declaration_path: Path, ledger: Path) -> dict:
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    required = {"frozen_at", "evidence_observed_through", "benchmark_strategy", "primary_cost_bps", "hypotheses"}
    if not required.issubset(declaration) or not declaration["hypotheses"]:
        raise ValueError("incomplete focused condition declaration")
    declaration_hash = _hash(declaration)
    source_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        required_members = {"tier1_manifest.json", "tier1_daily.csv", "tier1_market_conditions.csv"}
        if not required_members.issubset(names):
            raise ValueError(f"archive missing required members: {sorted(required_members - names)}")
        manifest = json.loads(bundle.read("tier1_manifest.json"))
        daily = _daily_returns(_rows(bundle, "tier1_daily.csv"), declaration["primary_cost_bps"])
        conditions = {row["date"]: row for row in _rows(bundle, "tier1_market_conditions.csv")}
    config_hash = _hash(manifest["config"])
    benchmark = declaration["benchmark_strategy"]
    strategies = {item["strategy"] for item in declaration["hypotheses"]}
    missing = sorted(({benchmark} | strategies).difference(daily))
    if missing:
        raise ValueError(f"archive is missing declared return series: {missing}")

    existing = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()] if ledger.is_file() else []
    if existing:
        _validate_existing(existing)
        if existing[0]["declaration_sha256"] != declaration_hash:
            raise ValueError("focused condition declaration changed")
        if existing[0]["config_sha256"] != config_hash:
            raise ValueError("strategy configuration changed")
    keys = {(row["hypothesis_id"], row["as_of_date"]) for row in existing}
    cumulative: dict[str, tuple[list[float], list[float]]] = {}
    for hypothesis in declaration["hypotheses"]:
        prior = [row for row in existing if row["hypothesis_id"] == hypothesis["id"] and row["condition_active"]]
        cumulative[hypothesis["id"]] = (
            [float(row["strategy_daily_return"]) for row in prior],
            [float(row["benchmark_daily_return"]) for row in prior],
        )

    appended = []
    cutoff = declaration["evidence_observed_through"]
    common_dates = sorted(set(conditions).intersection(daily[benchmark], *(daily[strategy] for strategy in strategies)))
    for day in (day for day in common_dates if day > cutoff):
        for hypothesis in sorted(declaration["hypotheses"], key=lambda item: item["id"]):
            key = (hypothesis["id"], day)
            if key in keys:
                continue
            state = conditions[day]
            bucket = state.get(hypothesis["dimension"])
            active = bucket == hypothesis["bucket"]
            strategy_return = daily[hypothesis["strategy"]][day]
            benchmark_return = daily[benchmark][day]
            strategy_active, benchmark_active = cumulative[hypothesis["id"]]
            if active:
                strategy_active.append(strategy_return); benchmark_active.append(benchmark_return)
            feature = hypothesis["dimension"].removesuffix("_bucket")
            payload = {
                "as_of_date": day,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "hypothesis_id": hypothesis["id"],
                "strategy": hypothesis["strategy"],
                "benchmark_strategy": benchmark,
                "dimension": hypothesis["dimension"],
                "declared_bucket": hypothesis["bucket"],
                "observed_bucket": bucket,
                "condition_value": float(state[feature]) if state.get(feature) not in (None, "") else None,
                "condition_active": active,
                "strategy_daily_return": strategy_return,
                "benchmark_daily_return": benchmark_return,
                "daily_excess": strategy_return - benchmark_return,
                "active_observations": len(strategy_active),
                "active_strategy_compounded_return": math.prod(1 + value for value in strategy_active) - 1,
                "active_benchmark_compounded_return": math.prod(1 + value for value in benchmark_active) - 1,
                "declaration_sha256": declaration_hash,
                "config_sha256": config_hash,
                "source_archive": archive.name,
                "source_sha256": source_hash,
                "previous_chain_sha256": (existing + appended)[-1]["chain_sha256"] if existing or appended else None,
                "paper_trading_approved": False,
            }
            payload["chain_sha256"] = _hash(payload)
            appended.append(payload); keys.add(key)
    if appended:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            for row in appended:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    all_rows = existing + appended
    return {
        "status": "appended" if appended else "unchanged",
        "new_rows": len(appended),
        "latest_date": max((row["as_of_date"] for row in all_rows), default=None),
        "active_new_rows": sum(bool(row["condition_active"]) for row in appended),
        "ledger": str(ledger),
        "chain_sha256": all_rows[-1]["chain_sha256"] if all_rows else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--declaration", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(append_observations(args.archive, args.declaration, args.ledger), indent=2))


if __name__ == "__main__":
    main()
