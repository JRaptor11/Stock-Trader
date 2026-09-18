"""Append forward-only baseline/state observations to a hash-chained ledger."""

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


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _rows(bundle, name):
    return list(csv.DictReader(io.TextIOWrapper(bundle.open(name), encoding="utf-8-sig")))


def _daily_returns(rows, cost_bps):
    result = {}
    selected = [row for row in rows if float(row["cost_bps"]) == float(cost_bps)]
    for strategy in sorted({row["strategy"] for row in selected}):
        prior = None
        result[strategy] = {}
        for row in sorted((row for row in selected if row["strategy"] == strategy),
                          key=lambda row: row["date"]):
            equity = float(row["equity"])
            if prior not in (None, 0.0):
                result[strategy][row["date"]] = equity / prior - 1.0
            prior = equity
    return result


def _validate_chain(rows):
    prior = None
    seen = set()
    for row in rows:
        key = (row["hypothesis_id"], row["as_of_date"])
        if key in seen or row.get("previous_chain_sha256") != prior:
            raise ValueError("invalid baseline forward ledger chain")
        stored = row.get("chain_sha256")
        unsigned = {name: value for name, value in row.items() if name != "chain_sha256"}
        if stored != _hash(unsigned):
            raise ValueError("invalid baseline forward ledger hash")
        prior = stored
        seen.add(key)


def append_observations(archive: Path, declaration_path: Path, ledger: Path) -> dict:
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    hypotheses = declaration.get("forward_baseline_hypotheses") or []
    forward_start = declaration.get("forward_start")
    benchmark = declaration.get("benchmark_strategy")
    primary = declaration.get("primary_cost_bps")
    if not hypotheses or not forward_start or not benchmark or primary is None:
        raise ValueError("declaration must freeze baseline hypotheses, start, benchmark, and cost")
    declaration_hash = _hash(declaration)
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("tier1_manifest.json"))
        definition = json.loads(bundle.read("tier1_market_state_definition.json"))
        if int(definition["state_change_confirmation_sessions"]) != int(
                declaration["state_confirmation_sessions"]):
            raise ValueError("archive state confirmation differs from frozen declaration")
        daily = _daily_returns(_rows(bundle, "tier1_daily.csv"), primary)
        labels = {row["date"]: row for row in _rows(bundle, "tier1_market_state_labels.csv")}
    config_hash = _hash(manifest["config"])
    strategies = {row["strategy"] for row in hypotheses}
    missing = sorted(({benchmark} | strategies).difference(daily))
    if missing:
        raise ValueError(f"archive is missing declared strategies: {missing}")
    existing = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
                if line.strip()] if ledger.is_file() else []
    _validate_chain(existing)
    if existing and (existing[0]["declaration_sha256"] != declaration_hash
                     or existing[0]["config_sha256"] != config_hash):
        raise ValueError("frozen baseline declaration or configuration changed")
    keys = {(row["hypothesis_id"], row["as_of_date"]) for row in existing}
    cumulative = {}
    for hypothesis in hypotheses:
        prior = [row for row in existing if row["hypothesis_id"] == hypothesis["hypothesis_id"]
                 and row["state_active"]]
        cumulative[hypothesis["hypothesis_id"]] = (
            [float(row["strategy_daily_return"]) for row in prior],
            [float(row["benchmark_daily_return"]) for row in prior],
        )
    common = sorted(set(labels).intersection(daily[benchmark],
                                             *(daily[name] for name in strategies)))
    appended = []
    for day in (day for day in common if day >= forward_start):
        for hypothesis in sorted(hypotheses, key=lambda row: row["hypothesis_id"]):
            key = (hypothesis["hypothesis_id"], day)
            if key in keys:
                continue
            label = labels[day]
            active = label["core_state"] == hypothesis["core_state"]
            strategy_return = daily[hypothesis["strategy"]][day]
            benchmark_return = daily[benchmark][day]
            strategy_active, benchmark_active = cumulative[hypothesis["hypothesis_id"]]
            if active:
                strategy_active.append(strategy_return)
                benchmark_active.append(benchmark_return)
            payload = {
                "hypothesis_id": hypothesis["hypothesis_id"], "as_of_date": day,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "strategy": hypothesis["strategy"], "benchmark_strategy": benchmark,
                "declared_core_state": hypothesis["core_state"],
                "observed_core_state": label["core_state"], "state_active": active,
                "state_changed": str(label.get("state_changed", "")).lower() == "true",
                "pending_core_state": label.get("pending_core_state") or None,
                "confirmation_sessions": int(declaration["state_confirmation_sessions"]),
                "strategy_daily_return": strategy_return,
                "benchmark_daily_return": benchmark_return,
                "daily_excess": strategy_return - benchmark_return,
                "active_observations": len(strategy_active),
                "active_strategy_compounded_return": math.prod(1 + value for value in strategy_active) - 1,
                "active_benchmark_compounded_return": math.prod(1 + value for value in benchmark_active) - 1,
                "declaration_sha256": declaration_hash, "config_sha256": config_hash,
                "source_archive": archive.name, "source_sha256": archive_hash,
                "previous_chain_sha256": (existing + appended)[-1]["chain_sha256"]
                if existing or appended else None,
                "paper_trading_approved": False,
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
        "latest_date": max((row["as_of_date"] for row in rows), default=None),
        "chain_sha256": rows[-1]["chain_sha256"] if rows else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--declaration", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(append_observations(args.archive, args.declaration, args.ledger), indent=2))


if __name__ == "__main__":
    main()
