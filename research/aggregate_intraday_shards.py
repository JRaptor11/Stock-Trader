"""Aggregate non-overlapping intraday shard archives into one audited result."""

from __future__ import annotations

import argparse, csv, io, json, zipfile
from pathlib import Path

from research.intraday_strategy_replay import _stability_summary, _write_csv_member
from research.intraday_validation import (
    benchmark_scorecards, cost_sensitivity, nested_regime_walk_forward, portfolio_daily_series,
    walk_forward_trade_scorecards,
)
from research.market_conditions import condition_scorecards
from research.portfolio_accounting import allocate_trades


def _rows(bundle, name):
    if name not in bundle.namelist(): return []
    text=bundle.read(name).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text))) if text.strip() else []


def _row_day(row):
    value=row.get("date") or row.get("timestamp") or row.get("entry_timestamp")
    return str(value or "")[:10]


def _within_evaluation(row, start, end):
    day=_row_day(row)
    return bool(day and start <= day <= end)


def _number(row, key):
    value=row.get(key)
    if value not in (None, ""): row[key]=float(value)


def _aggregate_stability(rows, initial_cash):
    grouped={}
    for row in rows:
        key=(row["strategy"],row["variant"])
        item=grouped.setdefault(key,{"strategy":row["strategy"],"variant":row["variant"],"parameter":row.get("parameter"),"value":row.get("value"),"accepted_trades":0,"realized_pnl":0.0,"weighted_trade_return":0.0,"winning_trades":0.0})
        count=int(float(row.get("accepted_trades") or 0)); portfolio_return=float(row.get("portfolio_return") or 0); mean=row.get("mean_trade_return"); win=row.get("win_rate")
        item["accepted_trades"]+=count; item["realized_pnl"]+=portfolio_return*initial_cash
        if mean not in (None, ""): item["weighted_trade_return"]+=float(mean)*count
        if win not in (None, ""): item["winning_trades"]+=float(win)*count
    result=[]
    for item in grouped.values():
        count=item.pop("accepted_trades"); pnl=item.pop("realized_pnl"); weighted=item.pop("weighted_trade_return"); wins=item.pop("winning_trades")
        result.append({**item,"accepted_trades":count,"portfolio_return":pnl/initial_cash,"mean_trade_return":weighted/count if count else None,"win_rate":wins/count if count else None})
    baselines={row["strategy"]:row for row in result if row["variant"]=="baseline"}
    for row in result:
        baseline=baselines[row["strategy"]]
        row["return_delta_vs_baseline"]=row["portfolio_return"]-baseline["portfolio_return"]
        row["same_return_sign_as_baseline"]=(row["portfolio_return"]>=0)==(baseline["portfolio_return"]>=0)
    return sorted(result,key=lambda row:(row["strategy"],row["variant"]!="baseline",row["variant"]))


def aggregate(archives: list[Path], output: Path) -> Path:
    manifests=[]; tables={name:[] for name in ("intraday_signals.csv","intraday_matched_controls.csv","intraday_market_conditions.csv","intraday_benchmark_daily.csv")}; raw_trades=[]; stability_rows=[]; ranges=[]
    for archive in archives:
        with zipfile.ZipFile(archive) as bundle:
            manifest=json.loads(bundle.read("intraday_manifest.json")); manifests.append(manifest)
            bounds=manifest.get("evaluation_range") or {}; start=bounds.get("start"); end=bounds.get("end"); ranges.append((start,end,archive.name))
            if not start or not end or start>end: raise ValueError("every shard needs a valid evaluation range")
            for name in tables: tables[name].extend(row for row in _rows(bundle,name) if _within_evaluation(row,start,end))
            raw_trades.extend(row for row in _rows(bundle,"intraday_trades.csv") if _within_evaluation(row,start,end))
            stability_rows.extend(_rows(bundle,"intraday_parameter_stability.csv"))
    identities={(m["experiment"].get("hypothesis_id"),m["config"].get("strategy_names") and tuple(m["config"]["strategy_names"]),m["config"].get("cost_bps_per_side"),(m.get("universe") or {}).get("sha256")) for m in manifests}
    if len(identities)!=1: raise ValueError("shard strategy identities differ")
    ordered=sorted(ranges)
    if any(ordered[i][0]<=ordered[i-1][1] for i in range(1,len(ordered))): raise ValueError("shard evaluation ranges overlap")
    config=manifests[0]["config"]
    for row in raw_trades:
        for key in ("net_return","entry_price","exit_price","quantity","gross_return"): _number(row,key)
        for key in ("portfolio_status","portfolio_rejection_reason","allocated_notional","realized_pnl"): row.pop(key,None)
    trades,portfolio_curve=allocate_trades(raw_trades,initial_cash=float(config["initial_cash"]),target_notional=float(config["target_notional"]),maximum_positions=int(config["maximum_positions"]),maximum_symbol_pct=float(config["maximum_symbol_pct"]))
    trades=sorted(trades,key=lambda row:(row["entry_timestamp"],row["strategy"],row["symbol"]))
    session_dates=sorted({_row_day(row) for row in tables["intraday_market_conditions.csv"] if _row_day(row)})
    walk_forward=walk_forward_trade_scorecards(trades,int(config["walk_forward_train_sessions"]),int(config["walk_forward_test_sessions"]),int(config["walk_forward_step_sessions"]),float(config["initial_cash"]),session_dates=session_dates)
    daily_rows=portfolio_daily_series(trades,session_dates,config["strategy_names"],float(config["initial_cash"]),float(config["cost_bps_per_side"]))
    benchmark_rows=[]; benchmark_equity=float(config["initial_cash"])
    for row in sorted(tables.pop("intraday_benchmark_daily.csv"),key=lambda item:item["date"]):
        daily_return=float(row["daily_return"]); benchmark_equity*=1+daily_return
        benchmark_rows.append({**row,"daily_return":daily_return,"equity":benchmark_equity})
    performance,annual,bootstrap=benchmark_scorecards(daily_rows,benchmark_rows,float(config["initial_cash"]),int(config.get("family_trial_count") or len(config["strategy_names"])))
    condition_map={row["date"]:row for row in tables["intraday_market_conditions.csv"]}
    condition_single,condition_pairs=condition_scorecards(daily_rows,condition_map,float(config["cost_bps_per_side"]))
    nested_regime=nested_regime_walk_forward(trades,session_dates,int(config["walk_forward_train_sessions"]),int(config["walk_forward_test_sessions"]),int(config["walk_forward_step_sessions"]),float(config["initial_cash"]),int(config.get("family_trial_count") or len(config["strategy_names"])),strategies=config["strategy_names"])
    costs=cost_sensitivity(trades,float(config["initial_cash"]),current_cost_bps=float(config["cost_bps_per_side"]))
    stability=_aggregate_stability(stability_rows,float(config["initial_cash"])) if stability_rows else []
    stability_summary=_stability_summary(stability) if stability else []
    scorecards=[]
    for strategy in config["strategy_names"]:
        accepted=[row for row in trades if row["strategy"]==strategy and row.get("portfolio_status")=="accepted"]
        scorecards.append({"strategy":strategy,"accepted_trades":len(accepted),"portfolio_return":sum(row["realized_pnl"] for row in accepted)/float(config["initial_cash"]),"mean_trade_return":sum(row["net_return"] for row in accepted)/len(accepted) if accepted else None,"shard_count":len(archives)})
    aggregate_manifest={"engine":"intraday_strategy_isolation_shard_aggregate","source_archives":[path.name for path in archives],"evaluation_ranges":ordered,"strategy_identity":list(identities)[0],"config":config,"promotion_status":"development_only_until_all_data_gates_pass","shards_overlap_for_warmup_but_evaluation_ranges_do_not_overlap":True,"portfolio_accounting":"reconstructed chronologically across all evaluation shards","stability_accounting":"weighted aggregation of independently allocated shard variants","validation_semantics":"full-session walk-forward, training-only nested regime selection, causal condition scorecards, paired block bootstrap, SPY and cash comparisons"}
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED,compresslevel=1) as bundle:
        for name,rows in tables.items(): _write_csv_member(bundle,name,rows)
        _write_csv_member(bundle,"intraday_trades.csv",trades); _write_csv_member(bundle,"intraday_portfolio_events.csv",portfolio_curve)
        _write_csv_member(bundle,"intraday_parameter_stability.csv",stability); _write_csv_member(bundle,"intraday_parameter_stability_summary.csv",stability_summary)
        _write_csv_member(bundle,"intraday_walk_forward.csv",walk_forward); _write_csv_member(bundle,"intraday_nested_regime_walk_forward.csv",nested_regime)
        _write_csv_member(bundle,"intraday_daily.csv",daily_rows); _write_csv_member(bundle,"intraday_benchmark_daily.csv",benchmark_rows)
        _write_csv_member(bundle,"intraday_performance.csv",performance); _write_csv_member(bundle,"intraday_calendar_years.csv",annual)
        _write_csv_member(bundle,"intraday_cost_sensitivity.csv",costs); _write_csv_member(bundle,"intraday_block_bootstrap.csv",bootstrap); _write_csv_member(bundle,"intraday_condition_scorecards.csv",condition_single)
        _write_csv_member(bundle,"intraday_condition_pair_scorecards.csv",condition_pairs); _write_csv_member(bundle,"intraday_scorecard.csv",scorecards)
        bundle.writestr("intraday_aggregate_manifest.json",json.dumps(aggregate_manifest,indent=2,default=list))
    return output


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--archive",type=Path,action="append",required=True); parser.add_argument("--output",type=Path,required=True); args=parser.parse_args(); print(aggregate(args.archive,args.output))


if __name__=="__main__": main()
