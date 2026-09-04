"""Broker-free tournament for isolated intraday strategy mechanisms."""

from __future__ import annotations

import csv, gc, hashlib, io, json, math, shutil, statistics, zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from research.strategy_registry import registry_snapshot, validate_experiment_declaration
from research.execution_model import ExecutionAssumptions, entry_fill, exit_fill, net_return
from research.research_contracts import apply_security_master, audit_bar_csv, load_security_master, promotion_gate
from research.portfolio_accounting import allocate_trades
from research.market_conditions import causal_market_conditions
from research.statistical_safeguards import return_evidence
from research.intraday_validation import matched_controls, walk_forward_trade_scorecards
from research.market_events import blocked_event, load_market_events, market_event_audit
from research.point_in_time_fundamentals import fundamentals_audit, load_fundamentals, snapshot_at
from research.historical_replay import SpilledRows

STRATEGIES = ("OPENING_RANGE_BREAKOUT", "RELATIVE_VOLUME_BREAKOUT", "VWAP_MEAN_REVERSION")
STABILITY_NEIGHBORHOODS = {
    "OPENING_RANGE_BREAKOUT": (("opening_range_bars", 2), ("opening_range_bars", 4)),
    "RELATIVE_VOLUME_BREAKOUT": (("breakout_lookback_bars", 15), ("breakout_lookback_bars", 25)),
    "VWAP_MEAN_REVERSION": (("vwap_deviation_pct", .0125), ("vwap_deviation_pct", .0175)),
}


@dataclass(frozen=True)
class IntradayConfig:
    initial_cash: float = 100_000.0
    strategy_names: tuple[str, ...] = STRATEGIES
    opening_range_bars: int = 3
    breakout_lookback_bars: int = 20
    volume_baseline_sessions: int = 20
    minimum_baseline_sessions: int = 10
    breakout_buffer_bps: float = 5.0
    opening_breakout_rvol: float = 2.0
    continuation_rvol: float = 3.0
    vwap_deviation_pct: float = .015
    stop_loss_pct: float = .01
    profit_target_pct: float = .02
    maximum_holding_bars: int = 12
    minimum_price: float = 1.0
    maximum_price: float = 100.0
    minimum_average_daily_dollar_volume: float = 5_000_000.0
    target_notional: float = 10_000.0
    maximum_bar_participation: float = .01
    cost_bps_per_side: float = 10.0
    assumed_spread_bps: float = 10.0
    minimum_trades_for_promotion: int = 100
    security_master_available: bool = False
    survivorship_safe_universe: bool = False
    halt_luld_available: bool = False
    point_in_time_market_cap_available: bool = False
    point_in_time_float_available: bool = False
    minimum_market_cap: float | None = None
    maximum_market_cap: float | None = None
    minimum_float_shares: float | None = None
    maximum_float_shares: float | None = None
    maximum_positions: int = 5
    maximum_symbol_pct: float = .20
    discovery_end_date: str | None = None
    holdout_start_date: str | None = None
    family_trial_count: int = 3
    walk_forward_train_sessions: int = 252
    walk_forward_test_sessions: int = 63
    walk_forward_step_sessions: int = 63
    run_parameter_stability: bool = True

    def __post_init__(self):
        unknown = set(self.strategy_names) - set(STRATEGIES)
        if unknown: raise ValueError(f"unknown intraday strategies: {sorted(unknown)}")
        if not self.strategy_names or len(set(self.strategy_names)) != len(self.strategy_names):
            raise ValueError("strategy_names must be nonempty and unique")
        for name in ("opening_range_bars", "breakout_lookback_bars", "volume_baseline_sessions", "minimum_baseline_sessions", "maximum_holding_bars"):
            if getattr(self, name) < 1: raise ValueError(f"{name} must be positive")
        if not 0 < self.maximum_bar_participation <= 1:
            raise ValueError("maximum_bar_participation must be in (0, 1]")
        if self.initial_cash <= 0 or self.maximum_positions < 1 or not 0 < self.maximum_symbol_pct <= 1:
            raise ValueError("portfolio limits must be positive and bounded")
        if bool(self.discovery_end_date) != bool(self.holdout_start_date) or (self.discovery_end_date and self.discovery_end_date >= self.holdout_start_date):
            raise ValueError("chronological discovery and holdout dates must be complete and nonoverlapping")
        if self.family_trial_count < len(self.strategy_names): raise ValueError("family_trial_count cannot undercount tested strategies")
        if min(self.walk_forward_train_sessions,self.walk_forward_test_sessions,self.walk_forward_step_sessions)<1: raise ValueError("walk-forward session counts must be positive")
        for low,high in ((self.minimum_market_cap,self.maximum_market_cap),(self.minimum_float_shares,self.maximum_float_shares)):
            if low is not None and low<=0 or high is not None and high<=0: raise ValueError("point-in-time fundamental bounds must be positive")
            if low is not None and high is not None and low>high: raise ValueError("point-in-time fundamental bounds are inverted")


def config_from_job(job: dict) -> IntradayConfig:
    supplied = dict(job.get("intraday_config") or {}); allowed = set(IntradayConfig.__dataclass_fields__)
    if set(supplied) - allowed: raise ValueError(f"unknown intraday_config fields: {sorted(set(supplied)-allowed)}")
    if "strategy_names" in supplied: supplied["strategy_names"] = tuple(supplied["strategy_names"])
    return IntradayConfig(**supplied)


def load_sessions(path: Path) -> dict:
    sessions = defaultdict(lambda: defaultdict(list))
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader=csv.DictReader(handle); required={"timestamp","symbol","open","high","low","close","volume"}
        if not required.issubset(reader.fieldnames or ()): raise ValueError(f"intraday CSV missing columns: {sorted(required-set(reader.fieldnames or ())) }")
        for row in reader:
            ts=datetime.fromisoformat(row["timestamp"].replace("Z","+00:00"))
            if ts.tzinfo is None: raise ValueError("bar timestamps must include a timezone")
            close=float(row["close"]); symbol=row["symbol"].upper()
            sessions[ts.astimezone(timezone.utc).date().isoformat()][symbol].append({"timestamp":ts.astimezone(timezone.utc).isoformat(), **{k:float(row[k]) for k in ("open","high","low","close","volume")}, "vwap":float(row.get("vwap") or close)})
    for symbols in sessions.values():
        for bars in symbols.values(): bars.sort(key=lambda row:row["timestamp"])
    return dict(sessions)


def _trigger(strategy, bars, index, rvol, config):
    bar=bars[index]
    if strategy=="OPENING_RANGE_BREAKOUT":
        if index < config.opening_range_bars: return False,"opening_range_incomplete"
        level=max(row["high"] for row in bars[:config.opening_range_bars]); threshold=config.opening_breakout_rvol
    elif strategy=="RELATIVE_VOLUME_BREAKOUT":
        if index < config.breakout_lookback_bars: return False,"breakout_history_incomplete"
        level=max(row["high"] for row in bars[index-config.breakout_lookback_bars:index]); threshold=config.continuation_rvol
    else:
        return bar["close"] < bar["vwap"]*(1-config.vwap_deviation_pct),"below_vwap"
    return bar["close"] > level*(1+config.breakout_buffer_bps/10000) and rvol >= threshold,"price_volume_breakout"


def _exit(bars, entry_index, entry, strategy, config):
    stop=entry*(1-config.stop_loss_pct); target=entry*(1+config.profit_target_pct); end=min(len(bars)-1,entry_index+config.maximum_holding_bars-1)
    for index in range(entry_index,end+1):
        bar=bars[index]
        if bar["low"] <= stop: return index,stop,"stop"
        if strategy=="VWAP_MEAN_REVERSION" and bar["high"] >= bar["vwap"]: return index,bar["vwap"],"vwap_reversion"
        if bar["high"] >= target: return index,target,"target"
    return end,bars[end]["close"],"time_or_close"


def _simulate(config, sessions, conditions, market_events=(), fundamentals=None, progress_callback=None, stage="intraday_baseline", collect_signals=True, signal_sink=None):
    profiles=defaultdict(list); daily_dollars=defaultdict(list); trades=[]; signals=signal_sink if signal_sink is not None else []; fundamental_cache={}
    assumptions=ExecutionAssumptions(config.cost_bps_per_side,config.assumed_spread_bps,config.maximum_bar_participation)
    session_days=sorted(sessions); progress_interval=max(1,len(session_days)//100)
    for day_number,day in enumerate(session_days,start=1):
        for symbol,bars in sorted(sessions[day].items()):
            prior=profiles[symbol][-config.volume_baseline_sessions:]; dollars=daily_dollars[symbol][-config.volume_baseline_sessions:]
            average_dollars=statistics.fmean(dollars) if dollars else 0.; cumulative=0.; traded=set()
            for index,bar in enumerate(bars[:-1]):
                cumulative+=bar["volume"]; comparable=[sum(p[:index+1]) for p in prior if len(p)>index]; expected=statistics.fmean(comparable) if comparable else 0.; rvol=cumulative/expected if expected else 0.
                for strategy in config.strategy_names:
                    triggered,reason=_trigger(strategy,bars,index,rvol,config)
                    if not triggered: continue
                    entry_bar=bars[index+1]; capacity=entry_bar["open"]*entry_bar["volume"]*config.maximum_bar_participation
                    event_block=blocked_event(symbol,bar["timestamp"],market_events) or blocked_event(symbol,entry_bar["timestamp"],market_events)
                    fundamental_key=(symbol,entry_bar["timestamp"])
                    if fundamentals is not None and fundamental_key not in fundamental_cache: fundamental_cache[fundamental_key]=snapshot_at(fundamentals,symbol,entry_bar["timestamp"])
                    fundamental=fundamental_cache.get(fundamental_key)
                    fundamental_block=fundamentals is not None and fundamental is None
                    if fundamental:
                        fundamental_block=fundamental_block or (config.minimum_market_cap is not None and fundamental["market_cap"]<config.minimum_market_cap) or (config.maximum_market_cap is not None and fundamental["market_cap"]>config.maximum_market_cap) or (config.minimum_float_shares is not None and fundamental["float_shares"]<config.minimum_float_shares) or (config.maximum_float_shares is not None and fundamental["float_shares"]>config.maximum_float_shares)
                    eligible=not event_block and not fundamental_block and len(prior)>=config.minimum_baseline_sessions and config.minimum_price<=entry_bar["open"]<=config.maximum_price and average_dollars>=config.minimum_average_daily_dollar_volume and capacity>=config.target_notional
                    signal={"date":day,"symbol":symbol,"strategy":strategy,"signal_timestamp":bar["timestamp"],"entry_timestamp":entry_bar["timestamp"],"signal_bar_index":index,"entry_bar_index":index+1,"relative_volume":rvol,"average_daily_dollar_volume":average_dollars,"entry_bar_capacity":capacity,"eligible":eligible,"signal_reason":reason,"market_event_block":event_block,"fundamental_block":fundamental_block,"fundamental_known_at":fundamental["known_at"] if fundamental else None,"fundamental_effective_date":fundamental["effective_date"] if fundamental else None,"point_in_time_market_cap":fundamental["market_cap"] if fundamental else None,"point_in_time_float_shares":fundamental["float_shares"] if fundamental else None,"point_in_time_market_cap_available":fundamental is not None,"point_in_time_float_available":fundamental is not None,**{f"market_{k}":v for k,v in conditions.get(day,{}).items() if k!="date"}}
                    if collect_signals: signals.append(signal)
                    if not eligible or strategy in traded: continue
                    entry,fill=entry_fill(entry_bar["open"],entry_bar["volume"],config.target_notional,assumptions)
                    if entry is None: continue
                    traded.add(strategy); exit_index,raw_exit,exit_reason=_exit(bars,index+1,entry,strategy,config); exit_price=exit_fill(raw_exit,assumptions)
                    trades.append({**signal,"exit_timestamp":bars[exit_index]["timestamp"],"entry_price":entry,"exit_price":exit_price,"gross_return":exit_price/entry-1,"net_return":net_return(entry,exit_price,assumptions),"exit_reason":exit_reason,"holding_bars":exit_index-index})
            profiles[symbol].append([row["volume"] for row in bars]); daily_dollars[symbol].append(sum(row["close"]*row["volume"] for row in bars))
        if progress_callback and (day_number==len(session_days) or day_number%progress_interval==0):
            progress_callback({"stage":stage,"stage_completed_sessions":day_number,"stage_total_sessions":len(session_days),"stage_percent_complete":round(day_number/len(session_days)*100,2)})
    return trades,signals


def _write_csv_member(bundle, name, rows):
    iterator=iter(rows)
    try: first=next(iterator)
    except StopIteration:
        bundle.writestr(name,""); return
    with bundle.open(name,"w") as raw:
        with io.TextIOWrapper(raw,encoding="utf-8",newline="") as text:
            writer=csv.DictWriter(text,fieldnames=list(first)); writer.writeheader(); writer.writerow(first)
            for row in iterator: writer.writerow(row)


def _parameter_stability(config, sessions, conditions, baseline_trades, market_events=(), fundamentals=None, initial_checkpoint=None, checkpoint_callback=None, progress_callback=None):
    if not config.run_parameter_stability: return []
    restored=list((initial_checkpoint or {}).get("stability_rows") or []); rows=[]; expected={(strategy,f"{name}={value}") for strategy in config.strategy_names for name,value in STABILITY_NEIGHBORHOODS[strategy]}
    try:
        keys=[(row["strategy"],row["variant"]) for row in restored]
        if len(keys)!=len(set(keys)) or any(key not in expected for key in keys) or any("portfolio_return" not in row for row in restored): restored=[]
    except (KeyError,TypeError): restored=[]
    completed={(row["strategy"],row["variant"]):row for row in restored}
    total_variants=sum(len(STABILITY_NEIGHBORHOODS[strategy]) for strategy in config.strategy_names); completed_variants=0
    for strategy in config.strategy_names:
        baseline=[row for row in baseline_trades if row["strategy"]==strategy and row["portfolio_status"]=="accepted"]
        values=[row["net_return"] for row in baseline]
        rows.append({"strategy":strategy,"variant":"baseline","parameter":"baseline","value":None,"accepted_trades":len(values),"portfolio_return":sum(row["realized_pnl"] for row in baseline)/config.initial_cash,"mean_trade_return":statistics.fmean(values) if values else None,"win_rate":sum(v>0 for v in values)/len(values) if values else None})
        variants=tuple((f"{name}={value}",name,value) for name,value in STABILITY_NEIGHBORHOODS[strategy])
        for variant,name,value in variants:
            if (strategy,variant) in completed:
                rows.append(completed[(strategy,variant)]); completed_variants+=1
                if progress_callback: progress_callback({"stage":"intraday_parameter_stability","stage_completed_variants":completed_variants,"stage_total_variants":total_variants,"stage_percent_complete":round(completed_variants/total_variants*100,2),"restored_variant":variant})
                continue
            settings=asdict(config); settings["strategy_names"]=(strategy,)
            if name: settings[name]=value
            variant_config=IntradayConfig(**settings)
            raw,_=_simulate(variant_config,sessions,conditions,market_events,fundamentals,progress_callback,f"intraday_stability_{strategy}_{variant}",collect_signals=False)
            accepted,_=allocate_trades(raw,initial_cash=config.initial_cash,target_notional=config.target_notional,maximum_positions=config.maximum_positions,maximum_symbol_pct=config.maximum_symbol_pct)
            accepted=[row for row in accepted if row["portfolio_status"]=="accepted"]
            values=[row["net_return"] for row in accepted]
            rows.append({"strategy":strategy,"variant":variant,"parameter":name or "baseline","value":value,"accepted_trades":len(values),"portfolio_return":sum(row["realized_pnl"] for row in accepted)/config.initial_cash,"mean_trade_return":statistics.fmean(values) if values else None,"win_rate":sum(v>0 for v in values)/len(values) if values else None})
            completed_variants+=1
            if checkpoint_callback: checkpoint_callback({"format_version":1,"completed_variants":completed_variants,"total_variants":total_variants,"stability_rows":[row for row in rows if row["variant"]!="baseline"]})
            if progress_callback: progress_callback({"stage":"intraday_parameter_stability","stage_completed_variants":completed_variants,"stage_total_variants":total_variants,"stage_percent_complete":round(completed_variants/total_variants*100,2)})
            del raw,accepted,values; gc.collect()
    baselines={row["strategy"]:row for row in rows if row["variant"]=="baseline"}
    for row in rows:
        base=baselines[row["strategy"]]
        row["return_delta_vs_baseline"]=row["portfolio_return"]-base["portfolio_return"]
        row["same_return_sign_as_baseline"]=(row["portfolio_return"]>=0)==(base["portfolio_return"]>=0)
    return rows


def _stability_summary(rows):
    summaries=[]
    for strategy in sorted({row["strategy"] for row in rows}):
        group=[row for row in rows if row["strategy"]==strategy]; baseline=next(row for row in group if row["variant"]=="baseline"); neighbors=[row for row in group if row["variant"]!="baseline"]
        summaries.append({"strategy":strategy,"baseline_portfolio_return":baseline["portfolio_return"],"neighbor_count":len(neighbors),"positive_neighbor_fraction":sum(row["portfolio_return"]>0 for row in neighbors)/len(neighbors) if neighbors else None,"same_sign_neighbor_fraction":sum(row["same_return_sign_as_baseline"] for row in neighbors)/len(neighbors) if neighbors else None,"worst_neighbor_return":min((row["portfolio_return"] for row in neighbors),default=None),"largest_absolute_delta":max((abs(row["return_delta_vs_baseline"]) for row in neighbors),default=None),"all_neighbors_same_sign":all(row["same_return_sign_as_baseline"] for row in neighbors) if neighbors else None})
    return summaries


def run_tournament(job: dict, bars_path: Path, archive_path: Path, source_sha256: str, progress_callback=None, initial_checkpoint=None, checkpoint_callback=None) -> Path:
    config=config_from_job(job); audit=audit_bar_csv(bars_path,feed=str(job.get("data_feed") or "unknown"),adjusted=bool(job.get("adjusted",False))); sessions=load_sessions(bars_path)
    master_path=Path(job["_security_master_path"]) if job.get("_security_master_path") else None
    master_rows=load_security_master(master_path) if master_path else []
    master_summary={"provided":False,"complete_observed_coverage":False,"classification_coverage":0.0}; master_diagnostics=[]
    if master_rows:
        sessions,master_summary,master_diagnostics=apply_security_master(sessions,master_rows); master_summary["provided"]=True
        master_summary["sha256"]=hashlib.sha256(master_path.read_bytes()).hexdigest()
        if not sessions: raise ValueError("security master excludes every observed symbol-session")
    events_path=Path(job["_market_events_path"]) if job.get("_market_events_path") else None
    market_events,event_coverage=load_market_events(events_path) if events_path else ([],{})
    event_audit=market_event_audit(sorted(sessions),market_events,event_coverage)
    if events_path: event_audit["sha256"]=hashlib.sha256(events_path.read_bytes()).hexdigest()
    fundamentals_path=Path(job["_fundamentals_path"]) if job.get("_fundamentals_path") else None
    fundamentals=load_fundamentals(fundamentals_path) if fundamentals_path else None
    fundamentals_summary,fundamentals_diagnostics=fundamentals_audit(sessions,fundamentals or {})
    fundamentals_summary["provided"]=fundamentals_path is not None
    if fundamentals_path: fundamentals_summary["sha256"]=hashlib.sha256(fundamentals_path.read_bytes()).hexdigest()
    assumptions=ExecutionAssumptions(config.cost_bps_per_side,config.assumed_spread_bps,config.maximum_bar_participation)
    daily={day:{symbol:{"open":bars[0]["open"],"high":max(r["high"] for r in bars),"low":min(r["low"] for r in bars),"close":bars[-1]["close"]} for symbol,bars in symbols.items()} for day,symbols in sessions.items()}
    universe=tuple(sorted({symbol for symbols in sessions.values() for symbol in symbols})); conditions=causal_market_conditions(sorted(sessions),daily,universe) if "SPY" in universe else {}
    signal_spill_root=archive_path.parent/f".{archive_path.name.lstrip('.')}.intraday-spill"; signal_spill=SpilledRows(signal_spill_root,"signals")
    trades,signals=_simulate(config,sessions,conditions,market_events,fundamentals,progress_callback,"intraday_baseline",signal_sink=signal_spill)
    portfolio_trades,portfolio_curve=allocate_trades(trades,initial_cash=config.initial_cash,target_notional=config.target_notional,maximum_positions=config.maximum_positions,maximum_symbol_pct=config.maximum_symbol_pct)
    controls=matched_controls(portfolio_trades,signals,sessions,assumptions,config.target_notional)
    walk_forward=walk_forward_trade_scorecards(portfolio_trades,config.walk_forward_train_sessions,config.walk_forward_test_sessions,config.walk_forward_step_sessions,config.initial_cash)
    stability=_parameter_stability(config,sessions,conditions,portfolio_trades,market_events,fundamentals,initial_checkpoint,checkpoint_callback,progress_callback); stability_summary=_stability_summary(stability)
    scorecards=[]
    for strategy in config.strategy_names:
        signal_values=[row["net_return"] for row in trades if row["strategy"]==strategy]; accepted=[row for row in portfolio_trades if row["strategy"]==strategy and row["portfolio_status"]=="accepted"]; values=[row["net_return"] for row in accepted]; pnl=sum(row["realized_pnl"] for row in accepted)
        scorecards.append({"strategy":strategy,"period":"full","eligible_signals":len(signal_values),"accepted_trades":len(values),"rejected_by_portfolio":len(signal_values)-len(values),"portfolio_return":pnl/config.initial_cash,"signal_compounded_return":math.prod(1+v for v in signal_values)-1 if signal_values else 0.,"mean_accepted_return":statistics.fmean(values) if values else None,"median_accepted_return":statistics.median(values) if values else None,"win_rate":sum(v>0 for v in values)/len(values) if values else None,"worst_return":min(values) if values else None,"best_return":max(values) if values else None,**return_evidence(values,config.family_trial_count)})
        if config.discovery_end_date:
            for period,predicate in (("discovery",lambda r:r["date"]<=config.discovery_end_date),("holdout",lambda r:r["date"]>=config.holdout_start_date)):
                subset=[r for r in accepted if predicate(r)]; subset_values=[r["net_return"] for r in subset]
                scorecards.append({"strategy":strategy,"period":period,"eligible_signals":None,"accepted_trades":len(subset_values),"rejected_by_portfolio":None,"portfolio_return":sum(r["realized_pnl"] for r in subset)/config.initial_cash,"signal_compounded_return":None,"mean_accepted_return":statistics.fmean(subset_values) if subset_values else None,"median_accepted_return":statistics.median(subset_values) if subset_values else None,"win_rate":sum(v>0 for v in subset_values)/len(subset_values) if subset_values else None,"worst_return":min(subset_values) if subset_values else None,"best_return":max(subset_values) if subset_values else None,**return_evidence(subset_values,config.family_trial_count)})
    evaluation=[row for row in scorecards if row["period"]==("holdout" if config.holdout_start_date else "full")]
    verified_master=bool(master_summary.get("complete_observed_coverage") and config.survivorship_safe_universe)
    gate=promotion_gate(audit=audit,security_master=verified_master,halt_luld=event_audit["halt_luld_complete"],corporate_actions=event_audit["corporate_actions_complete"],delistings=event_audit["delistings_complete"],point_in_time_cap=fundamentals_summary["market_cap_complete"],point_in_time_float=fundamentals_summary["float_complete"],minimum_trades_met=all(row["accepted_trades"]>=config.minimum_trades_for_promotion for row in evaluation))
    manifest={"engine":"intraday_strategy_isolation","source_sha256":source_sha256,"dataset_audit":asdict(audit),"security_master":master_summary,"market_events":event_audit,"point_in_time_fundamentals":fundamentals_summary,"survivorship_safe_universe_declared":config.survivorship_safe_universe,"promotion_gate":gate,"config":asdict(config),"experiment":validate_experiment_declaration(job.get("experiment")),"hypothesis_registry":registry_snapshot(),"strategies_combined":False,"execution_semantics":"completed-bar signal; next-bar-open entry; pessimistic same-bar stop precedence; halt and LULD intervals block signals and entries","validation_semantics":"expanding chronological walk-forward folds plus same-session, same-entry-bar, nearest-price non-signal controls","limitations":["a security master classifies observed bars but only a source-universe guarantee controls omitted-delisted-symbol survivorship bias","fundamental eligibility uses only snapshots known by the intended entry timestamp","delisting returns are audited for universe integrity but are not applied because this engine closes positions intraday","matched controls reduce timing and price differences but do not establish causal treatment effects"]}
    archive_path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive_path,"w",zipfile.ZIP_DEFLATED,compresslevel=1) as bundle:
        for name,rows in (("intraday_signals.csv",signals),("intraday_trades.csv",portfolio_trades),("intraday_matched_controls.csv",controls),("intraday_walk_forward.csv",walk_forward),("intraday_parameter_stability.csv",stability),("intraday_parameter_stability_summary.csv",stability_summary),("intraday_portfolio_events.csv",portfolio_curve),("intraday_market_conditions.csv",list(conditions.values())),("security_master_diagnostics.csv",master_diagnostics),("point_in_time_fundamentals_diagnostics.csv",fundamentals_diagnostics),("intraday_scorecard.csv",scorecards)):
            _write_csv_member(bundle,name,rows)
        bundle.writestr("intraday_manifest.json",json.dumps(manifest,indent=2,default=list))
    signal_spill.close(); signal_spill.path.unlink(missing_ok=True); shutil.rmtree(signal_spill_root,ignore_errors=True)
    return archive_path
