"""Append-only forward ledger for a frozen intraday strategy experiment."""
from __future__ import annotations

import argparse, csv, hashlib, io, json, zipfile
from datetime import datetime, timezone
from pathlib import Path

FROZEN_FIELDS=(
    "strategy_names","opening_range_bars","breakout_lookback_bars","volume_baseline_sessions",
    "minimum_baseline_sessions","breakout_buffer_bps","opening_breakout_rvol","continuation_rvol",
    "vwap_deviation_pct","stop_loss_pct","profit_target_pct","maximum_holding_bars","minimum_price",
    "maximum_price","minimum_average_daily_dollar_volume","target_notional","maximum_bar_participation",
    "cost_bps_per_side","assumed_spread_bps","maximum_positions","maximum_symbol_pct",
)


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":")).encode()


def _members(bundle):
    manifest_name="intraday_manifest.json" if "intraday_manifest.json" in bundle.namelist() else "intraday_aggregate_manifest.json"
    return manifest_name,"intraday_daily.csv"


def append_observation(archive: Path, ledger: Path, forward_start: str, strategy: str) -> dict:
    with zipfile.ZipFile(archive) as bundle:
        manifest_name,daily_name=_members(bundle); manifest=json.loads(bundle.read(manifest_name))
        daily=list(csv.DictReader(io.TextIOWrapper(bundle.open(daily_name),encoding="utf-8")))
        benchmark=list(csv.DictReader(io.TextIOWrapper(bundle.open("intraday_benchmark_daily.csv"),encoding="utf-8")))
    config=manifest["config"]; frozen={key:config.get(key) for key in FROZEN_FIELDS}; frozen["universe_sha256"]=(manifest.get("universe") or {}).get("sha256"); config_hash=hashlib.sha256(_canonical(frozen)).hexdigest()
    rows=sorted((row for row in daily if row["strategy"]==strategy and row["date"]>=forward_start),key=lambda row:row["date"])
    if not rows: raise ValueError("archive does not contain a forward observation for the selected strategy")
    existing=[]
    if ledger.is_file(): existing=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    if existing and existing[0]["config_sha256"]!=config_hash: raise ValueError("fixed intraday forward configuration changed")
    latest=rows[-1]
    if existing and latest["date"]<=existing[-1]["as_of_date"]:
        return {"status":"unchanged","as_of_date":latest["date"],"chain_sha256":existing[-1]["chain_sha256"]}
    benchmark_row=next((row for row in benchmark if row["date"]==latest["date"]),None)
    if benchmark_row is None: raise ValueError("archive does not contain a matching SPY benchmark observation")
    payload={"as_of_date":latest["date"],"recorded_at":datetime.now(timezone.utc).isoformat(),"strategy":strategy,
             "equity":float(latest["equity"]),"daily_return":float(latest["daily_return"]),"daily_pnl":float(latest["daily_pnl"]),
             "drawdown":float(latest["drawdown"]),"turnover":float(latest["turnover"]),"trades":int(latest["trades"]),
             "cumulative_return":float(latest["equity"])/float(config["initial_cash"])-1,
             "spy_daily_return":float(benchmark_row["daily_return"]),
             "spy_cumulative_return":float(benchmark_row["equity"])/float(config["initial_cash"])-1,
             "config_sha256":config_hash,"source_sha256":manifest.get("source_sha256"),"source_archive":archive.name,
             "previous_chain_sha256":existing[-1]["chain_sha256"] if existing else None,"paper_trading_approved":False}
    payload["chain_sha256"]=hashlib.sha256(_canonical(payload)).hexdigest(); ledger.parent.mkdir(parents=True,exist_ok=True)
    with ledger.open("a",encoding="utf-8") as handle: handle.write(json.dumps(payload,sort_keys=True)+"\n")
    return {"status":"appended",**payload}


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--archive",type=Path,required=True); parser.add_argument("--ledger",type=Path,required=True); parser.add_argument("--forward-start",required=True); parser.add_argument("--strategy",required=True); args=parser.parse_args()
    print(json.dumps(append_observation(args.archive,args.ledger,args.forward_start,args.strategy),indent=2))


if __name__=="__main__": main()
