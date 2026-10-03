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

FROZEN_STRATEGY_FIELDS = (
    "initial_cash", "universe_name", "market_state_universe_name",
    "benchmark_symbol", "cash_proxy_symbol",
    "rebalance_frequency", "volatility_lookback_days",
    "volatility_target_annualized", "trend_lookback_days",
    "momentum_lookbacks_days", "sector_holdings", "no_trade_band",
    "cost_ladder_bps", "primary_cost_bps", "discovery_end_date",
    "holdout_start_date",
)


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _frozen_projection(config: dict) -> dict:
    return {key: config[key] for key in FROZEN_STRATEGY_FIELDS if key in config}


def _validate_schema_compatible_config(
    *, current: dict, current_hash: str, existing: list[dict], ledger: Path
) -> str:
    """Keep a ledger stable across additive manifest-schema expansions."""
    if not existing or existing[0]["config_sha256"] == current_hash:
        return current_hash
    reference_name = existing[0].get("source_archive")
    reference_path = ledger.parent / reference_name if reference_name else None
    if not reference_path or not reference_path.is_file():
        raise ValueError("fixed forward configuration changed")
    with zipfile.ZipFile(reference_path) as bundle:
        reference = json.loads(bundle.read("tier1_manifest.json"))["config"]
    reference_hash = hashlib.sha256(_canonical(reference)).hexdigest()
    if reference_hash != existing[0]["config_sha256"]:
        raise ValueError("fixed forward reference archive does not match ledger")
    if _frozen_projection(reference) != _frozen_projection(current):
        raise ValueError("fixed forward configuration changed")
    return reference_hash


def _load_archive_rows(archive: Path, strategy: str) -> tuple[dict, dict, list[dict]]:
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("tier1_manifest.json"))
        daily = list(csv.DictReader(io.TextIOWrapper(bundle.open("tier1_daily.csv"), encoding="utf-8")))
    config = dict(manifest["config"])
    rows = [r for r in daily if r["strategy"] == strategy and float(r["cost_bps"]) == float(config["primary_cost_bps"])]
    rows.sort(key=lambda r: r["date"])
    return manifest, config, rows


def _existing_observations(ledger: Path) -> list[dict]:
    if not ledger.is_file():
        return []
    return [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_missing_observations(
    archive: Path,
    ledger: Path,
    forward_start: str,
    strategy: str = "SECTOR_ETF_ROTATION",
    variant: str = "g2-momentum-no-1m",
) -> dict:
    """Append every new daily observation in one archive, in date order."""
    manifest, config, rows = _load_archive_rows(archive, strategy)
    if not rows or rows[-1]["date"] < forward_start:
        raise ValueError("archive does not contain a forward observation")
    existing = _existing_observations(ledger)
    config_hash = hashlib.sha256(_canonical(config)).hexdigest()
    config_hash = _validate_schema_compatible_config(
        current=config, current_hash=config_hash, existing=existing, ledger=ledger
    )
    last_date = existing[-1]["as_of_date"] if existing else ""
    pending_indexes = [
        index
        for index, row in enumerate(rows)
        if row["date"] >= forward_start and row["date"] > last_date
    ]
    if not pending_indexes:
        return {
            "status": "unchanged",
            "as_of_date": rows[-1]["date"],
            "appended_count": 0,
            "chain_sha256": existing[-1]["chain_sha256"] if existing else None,
        }

    previous_chain = existing[-1]["chain_sha256"] if existing else None
    payloads = []
    for index in pending_indexes:
        row = rows[index]
        previous_equity = (
            float(rows[index - 1]["equity"])
            if index > 0
            else float(config["initial_cash"])
        )
        payload = {
            "as_of_date": row["date"],
            "recorded_at": datetime.now(UTC).isoformat(),
            "strategy": strategy,
            "variant": variant,
            "equity": float(row["equity"]),
            "daily_return": float(row["equity"]) / previous_equity - 1.0,
            "cash": float(row["cash"]),
            "positions": int(row["positions"]),
            "config_sha256": config_hash,
            "source_sha256": manifest["source_sha256"],
            "source_archive": archive.name,
            "previous_chain_sha256": previous_chain,
            "paper_trading_approved": False,
        }
        payload["chain_sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
        previous_chain = payload["chain_sha256"]
        payloads.append(payload)

    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return {
        "status": "appended",
        "appended_count": len(payloads),
        "first_appended_date": payloads[0]["as_of_date"],
        **payloads[-1],
    }


def append_observation(archive: Path, ledger: Path, forward_start: str,
                       strategy: str = "SECTOR_ETF_ROTATION",
                       variant: str = "g2-momentum-no-1m") -> dict:
    """Backward-compatible name; now catches up every missing session."""
    return append_missing_observations(
        archive, ledger, forward_start, strategy, variant
    )


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--archive",type=Path,required=True); parser.add_argument("--ledger",type=Path,required=True); parser.add_argument("--forward-start",default="2026-09-03"); parser.add_argument("--strategy",default="SECTOR_ETF_ROTATION"); parser.add_argument("--variant",default="g2-momentum-no-1m"); args=parser.parse_args()
    print(json.dumps(append_observation(args.archive,args.ledger,args.forward_start,args.strategy,args.variant),indent=2))


if __name__ == "__main__": main()
