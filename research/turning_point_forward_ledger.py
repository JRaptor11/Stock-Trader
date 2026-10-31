"""Append-only broker-free ledger for frozen turning-point forward hypotheses."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


UTC = timezone.utc


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _rows(bundle: zipfile.ZipFile, name: str) -> list[dict]:
    return list(csv.DictReader(io.TextIOWrapper(bundle.open(name), encoding="utf-8-sig")))


def _daily_returns(rows: list[dict], costs: set[float]) -> dict[tuple[str, float], dict[str, float]]:
    grouped: dict[tuple[str, float], list[dict]] = defaultdict(list)
    for row in rows:
        cost = float(row["cost_bps"])
        if cost in costs:
            grouped[(row["strategy"], cost)].append(row)
    result = {}
    for key, values in grouped.items():
        previous = None
        path = {}
        for row in sorted(values, key=lambda item: item["date"]):
            equity = float(row["equity"])
            if previous not in (None, 0.0):
                path[row["date"]] = equity / previous - 1.0
            previous = equity
        result[key] = path
    return result


def _read_ledger(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _validate_chain(rows: list[dict]) -> None:
    previous = None
    keys = set()
    for row in rows:
        key = (row["hypothesis_id"], float(row["cost_bps"]), row["session"])
        if key in keys or row.get("previous_chain_sha256") != previous:
            raise ValueError("invalid turning-point forward ledger chain")
        stored = row.get("chain_sha256")
        unsigned = {name: value for name, value in row.items() if name != "chain_sha256"}
        if stored != _hash(unsigned):
            raise ValueError("invalid turning-point forward ledger hash")
        previous = stored
        keys.add(key)


def append_observations(archive: Path, declaration_path: Path, ledger: Path) -> dict:
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    hypotheses = declaration.get("forward_hypotheses") or []
    forward_start = declaration.get("forward_start")
    benchmark = declaration.get("benchmark_strategy")
    target_state = declaration.get("target_confirmed_trend_state")
    costs = {float(value) for value in declaration.get("cost_ladder_bps", [])}
    if not hypotheses or not forward_start or not benchmark or not target_state or not costs:
        raise ValueError("declaration must freeze hypotheses, start, benchmark, state, and costs")
    if declaration.get("ledger", {}).get("broker_orders_enabled") is not False:
        raise ValueError("turning-point forward ledger must remain broker-free")

    declaration_hash = _hash(declaration)
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("tier1_manifest.json"))
        if manifest.get("experiment", {}).get("hypothesis_id") != declaration["study_id"]:
            raise ValueError("archive experiment does not match frozen forward study")
        definition = json.loads(bundle.read("tier1_market_state_definition.json"))
        if int(definition["state_change_confirmation_sessions"]) != int(
            declaration["state_confirmation_sessions"]
        ):
            raise ValueError("archive confirmation window differs from declaration")
        daily = _daily_returns(_rows(bundle, "tier1_daily.csv"), costs)
        labels = {row["date"]: row for row in _rows(bundle, "tier1_market_state_labels.csv")}
        cycles = {
            row["date"]: row["turning_point_cycle"]
            for row in _rows(bundle, "tier1_turning_point_cycle_labels.csv")
        }

    config_hash = _hash(manifest["config"])
    strategies = {row["strategy"] for row in hypotheses}
    missing = sorted(
        (strategy, cost) for strategy in ({benchmark} | strategies) for cost in costs
        if (strategy, cost) not in daily
    )
    if missing:
        raise ValueError(f"archive is missing declared strategy-cost paths: {missing}")

    existing = _read_ledger(ledger)
    _validate_chain(existing)
    if existing and (
        existing[0]["declaration_sha256"] != declaration_hash
        or existing[0]["config_sha256"] != config_hash
    ):
        raise ValueError("frozen turning-point declaration or configuration changed")
    keys = {
        (row["hypothesis_id"], float(row["cost_bps"]), row["session"])
        for row in existing
    }
    cumulative = defaultdict(lambda: ([], []))
    for row in existing:
        if row["state_active"]:
            strategy_values, benchmark_values = cumulative[
                (row["hypothesis_id"], float(row["cost_bps"]))
            ]
            strategy_values.append(float(row["strategy_daily_return"]))
            benchmark_values.append(float(row["benchmark_daily_return"]))

    common = set(labels) & set(cycles)
    for strategy in ({benchmark} | strategies):
        for cost in costs:
            common &= set(daily[(strategy, cost)])
    appended = []
    for day in sorted(day for day in common if day >= forward_start):
        label = labels[day]
        for hypothesis in sorted(hypotheses, key=lambda item: item["hypothesis_id"]):
            for cost in sorted(costs):
                key = (hypothesis["hypothesis_id"], cost, day)
                if key in keys:
                    continue
                strategy_return = daily[(hypothesis["strategy"], cost)][day]
                benchmark_return = daily[(benchmark, cost)][day]
                active = label["trend_state"] == target_state
                strategy_active, benchmark_active = cumulative[(hypothesis["hypothesis_id"], cost)]
                if active:
                    strategy_active.append(strategy_return)
                    benchmark_active.append(benchmark_return)
                prior_hash = (existing + appended)[-1]["chain_sha256"] if existing or appended else None
                payload = {
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "role": hypothesis["role"], "strategy": hypothesis["strategy"],
                    "cost_bps": cost, "session": day,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "declared_trend_state": target_state,
                    "observed_trend_state": label["trend_state"],
                    "observed_core_state": label["core_state"],
                    "turning_point_cycle": cycles[day], "state_active": active,
                    "strategy_daily_return": strategy_return,
                    "benchmark_strategy": benchmark,
                    "benchmark_daily_return": benchmark_return,
                    "daily_excess": strategy_return - benchmark_return,
                    "active_observations": len(strategy_active),
                    "active_strategy_compounded_return": math.prod(
                        1.0 + value for value in strategy_active
                    ) - 1.0,
                    "active_benchmark_compounded_return": math.prod(
                        1.0 + value for value in benchmark_active
                    ) - 1.0,
                    "declaration_sha256": declaration_hash,
                    "config_sha256": config_hash,
                    "source_archive": archive.name, "source_sha256": archive_hash,
                    "previous_chain_sha256": prior_hash,
                    "broker_orders_enabled": False, "paper_trading_approved": False,
                    "live_trading_approved": False,
                }
                payload["chain_sha256"] = _hash(payload)
                appended.append(payload)
                keys.add(key)
    if appended:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            for row in appended:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    rows = existing + appended
    return {
        "status": "appended" if appended else "unchanged",
        "new_rows": len(appended), "observations": len(rows),
        "active_new_rows": sum(row["state_active"] for row in appended),
        "latest_session": max((row["session"] for row in rows), default=None),
        "chain_sha256": rows[-1]["chain_sha256"] if rows else None,
        "broker_orders_enabled": False,
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
