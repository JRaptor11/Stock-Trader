"""Append hash-chained forward observations for frozen tactical hypotheses."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_chain(rows):
    previous = None
    seen = set()
    for row in rows:
        key = (row["hypothesis_id"], row["entry_date"])
        if key in seen or row.get("previous_chain_sha256") != previous:
            raise ValueError("invalid tactical forward ledger chain")
        stored = row.get("chain_sha256")
        if stored != _hash({key: value for key, value in row.items() if key != "chain_sha256"}):
            raise ValueError("invalid tactical forward ledger hash")
        seen.add(key); previous = stored


def append_observations(archive: Path, declaration_path: Path, ledger: Path) -> dict:
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    hypotheses = declaration.get("forward_tactical_hypotheses") or []
    forward_start = declaration.get("forward_start")
    if not hypotheses or not forward_start:
        raise ValueError("declaration must freeze forward_start and tactical hypotheses")
    declaration_hash = _hash(declaration)
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("tier1_manifest.json"))
        comparisons = list(csv.DictReader(io.TextIOWrapper(
            bundle.open("tier1_tactical_horizon_comparisons.csv"), encoding="utf-8-sig"
        )))
    config_hash = _hash(manifest["config"])
    existing = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
                if line.strip()] if ledger.is_file() else []
    _validate_chain(existing)
    if existing and (existing[0]["declaration_sha256"] != declaration_hash
                     or existing[0]["config_sha256"] != config_hash):
        raise ValueError("frozen tactical forward declaration or configuration changed")
    keys = {(row["hypothesis_id"], row["entry_date"]) for row in existing}
    appended = []
    for hypothesis in hypotheses:
        pending = hypothesis["transition_phase"] == "pending"
        matches = [row for row in comparisons
                   if row["strategy"] == hypothesis["strategy"]
                   and row["core_state"] == hypothesis["core_state"]
                   and int(row["horizon_sessions"]) == int(hypothesis["horizon_sessions"])
                   and bool(row["pending_core_state_at_entry"]) == pending
                   and row["entry_date"] >= forward_start]
        for row in sorted(matches, key=lambda item: item["entry_date"]):
            key = (hypothesis["hypothesis_id"], row["entry_date"])
            if key in keys:
                continue
            payload = {
                "hypothesis_id": hypothesis["hypothesis_id"],
                "entry_date": row["entry_date"],
                "comparison_end_date": row["comparison_end_date"],
                "strategy": row["strategy"], "core_state": row["core_state"],
                "transition_phase": hypothesis["transition_phase"],
                "horizon_sessions": int(row["horizon_sessions"]),
                "tactical_net_return": float(row["tactical_net_return"]),
                "baseline_return": float(row["baseline_return"]),
                "incremental_return_vs_baseline": float(row["incremental_return_vs_baseline"]),
                "tactical_beat_baseline": row["tactical_beat_baseline"] == "True",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "declaration_sha256": declaration_hash, "config_sha256": config_hash,
                "source_archive": archive.name, "source_sha256": archive_hash,
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
    rows = existing + appended
    return {"status": "appended" if appended else "unchanged", "new_rows": len(appended),
            "observations": len(rows), "chain_sha256": rows[-1]["chain_sha256"] if rows else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--declaration", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(append_observations(args.archive, args.declaration, args.ledger), indent=2))


if __name__ == "__main__":
    main()
