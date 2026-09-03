"""Append-only, hash-chained forward observations for a fixed Tier 1 model."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


UTC = timezone.utc


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def append_observation(archive: Path, ledger: Path, forward_start: str) -> dict:
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("tier1_manifest.json"))
        daily = list(csv.DictReader(io.TextIOWrapper(bundle.open("tier1_daily.csv"), encoding="utf-8")))
    config = dict(manifest["config"]); config_hash = hashlib.sha256(_canonical(config)).hexdigest()
    rows = [r for r in daily if r["strategy"] == "SECTOR_ETF_ROTATION" and float(r["cost_bps"]) == float(config["primary_cost_bps"])]
    rows.sort(key=lambda r: r["date"])
    if not rows or rows[-1]["date"] < forward_start:
        raise ValueError("archive does not contain a forward observation")
    existing = []
    if ledger.is_file():
        existing = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    if existing and existing[0]["config_sha256"] != config_hash:
        raise ValueError("fixed forward configuration changed")
    latest_date = rows[-1]["date"]
    if existing and latest_date <= existing[-1]["as_of_date"]:
        return {"status": "unchanged", "as_of_date": latest_date, "chain_sha256": existing[-1]["chain_sha256"]}
    previous_equity = float(rows[-2]["equity"]) if len(rows) > 1 else float(config["initial_cash"])
    payload = {
        "as_of_date": latest_date, "recorded_at": datetime.now(UTC).isoformat(),
        "strategy": "SECTOR_ETF_ROTATION", "variant": "g2-momentum-no-1m",
        "equity": float(rows[-1]["equity"]),
        "daily_return": float(rows[-1]["equity"]) / previous_equity - 1.0,
        "cash": float(rows[-1]["cash"]), "positions": int(rows[-1]["positions"]),
        "config_sha256": config_hash,
        "source_sha256": manifest["source_sha256"],
        "source_archive": archive.name,
        "previous_chain_sha256": existing[-1]["chain_sha256"] if existing else None,
        "paper_trading_approved": False,
    }
    payload["chain_sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return {"status": "appended", **payload}


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--archive",type=Path,required=True); parser.add_argument("--ledger",type=Path,required=True); parser.add_argument("--forward-start",default="2026-09-03"); args=parser.parse_args()
    print(json.dumps(append_observation(args.archive,args.ledger,args.forward_start),indent=2))


if __name__ == "__main__": main()
