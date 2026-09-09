"""Matched controls and chronological validation diagnostics for intraday trades."""
from collections import defaultdict
import math, statistics
from research.execution_model import entry_fill, exit_fill, net_return
from research.statistical_safeguards import paired_block_bootstrap, return_evidence
from research.walk_forward import build_walk_forward_folds

REGIME_SELECTION_DIMENSIONS=(
    "market_trend_20d_return_bucket", "market_volatility_20d_bucket",
    "market_breadth_50d_bucket", "market_dispersion_20d_bucket",
)

def matched_controls(trades, signals, sessions, assumptions, target_notional):
    triggered={(r["date"],r["symbol"],r["strategy"]) for r in signals if r["eligible"]}; rows=[]
    for trade in trades:
        if trade.get("portfolio_status")!="accepted": continue
        day=trade["date"]; index=int(trade["entry_bar_index"]); candidates=[]
        for symbol,bars in sessions.get(day,{}).items():
            if symbol==trade["symbol"] or (day,symbol,trade["strategy"]) in triggered or index>=len(bars): continue
            price=bars[index]["open"]
            candidates.append((abs(math.log(max(price,1e-9)/trade["entry_price"])),symbol,bars))
        if not candidates: continue
        _,symbol,bars=min(candidates); entry,_=entry_fill(bars[index]["open"],bars[index]["volume"],target_notional,assumptions)
        if entry is None: continue
        end=min(len(bars)-1,index+int(trade["holding_bars"])-1); exit_price=exit_fill(bars[end]["close"],assumptions)
        control=net_return(entry,exit_price,assumptions)
        rows.append({"date":day,"strategy":trade["strategy"],"signal_symbol":trade["symbol"],"control_symbol":symbol,"entry_timestamp":bars[index]["timestamp"],"signal_return":trade["net_return"],"control_return":control,"matched_excess_return":trade["net_return"]-control,"match":"same_session_same_bar_nearest_entry_price_non_signal"})
    return rows

def walk_forward_trade_scorecards(trades, min_train_sessions=252, test_sessions=63, step_sessions=63, initial_cash=100_000.0, session_dates=None):
    accepted=[r for r in trades if r.get("portfolio_status")=="accepted"]
    dates=sorted(set(session_dates)) if session_dates is not None else sorted({r["date"] for r in accepted})
    if len(dates)<min_train_sessions+test_sessions: return []
    folds=build_walk_forward_folds(dates,min_train_sessions=min_train_sessions,test_sessions=test_sessions,step_sessions=step_sessions,expanding=True); rows=[]
    for fold in folds:
        for strategy in sorted({r["strategy"] for r in accepted}):
            train=[r["net_return"] for r in accepted if r["strategy"]==strategy and fold.train_start<=r["date"]<=fold.train_end]
            test=[r["net_return"] for r in accepted if r["strategy"]==strategy and fold.test_start<=r["date"]<=fold.test_end]
            train_pnl=sum(float(r.get("realized_pnl") or 0) for r in accepted if r["strategy"]==strategy and fold.train_start<=r["date"]<=fold.train_end)
            test_pnl=sum(float(r.get("realized_pnl") or 0) for r in accepted if r["strategy"]==strategy and fold.test_start<=r["date"]<=fold.test_end)
            rows.append({"fold":fold.fold,"strategy":strategy,"train_start":fold.train_start,"train_end":fold.train_end,"test_start":fold.test_start,"test_end":fold.test_end,"train_trades":len(train),"test_trades":len(test),"train_mean_trade_return":statistics.fmean(train) if train else None,"test_mean_trade_return":statistics.fmean(test) if test else None,"train_portfolio_return":train_pnl/initial_cash,"test_portfolio_return":test_pnl/initial_cash})
    return rows


def portfolio_daily_series(trades, session_dates, strategies, initial_cash, cost_bps=10.0):
    accepted=[row for row in trades if row.get("portfolio_status")=="accepted"]
    pnl=defaultdict(float); turnover=defaultdict(float); counts=defaultdict(int)
    for row in accepted:
        key=(row["strategy"],row["date"]); pnl[key]+=float(row.get("realized_pnl") or 0)
        turnover[key]+=2*float(row.get("allocated_notional") or 0); counts[key]+=1
    rows=[]
    for strategy in sorted(strategies):
        equity=float(initial_cash); peak=equity
        for day in sorted(set(session_dates)):
            prior=equity; equity+=pnl[(strategy,day)]; peak=max(peak,equity)
            rows.append({"date":day,"strategy":strategy,"cost_bps":cost_bps,"equity":equity,
                         "daily_return":equity/prior-1 if prior else 0.,"daily_pnl":pnl[(strategy,day)],
                         "turnover":turnover[(strategy,day)]/prior if prior else 0.,
                         "trades":counts[(strategy,day)],"drawdown":equity/peak-1 if peak else None})
    return rows


def spy_benchmark_daily(daily_bars, session_dates, initial_cash):
    rows=[]; equity=float(initial_cash); prior_close=None; peak=equity
    for day in sorted(set(session_dates)):
        bar=daily_bars.get(day,{}).get("SPY")
        if not bar: continue
        change=float(bar["close"])/(prior_close if prior_close else float(bar["open"]))-1
        equity*=1+change; peak=max(peak,equity)
        rows.append({"date":day,"strategy":"SPY_BUY_HOLD","cost_bps":0.,"equity":equity,
                     "daily_return":change,"daily_pnl":None,"turnover":0.,"trades":0,
                     "drawdown":equity/peak-1 if peak else None})
        prior_close=float(bar["close"])
    return rows


def _drawdown_profile(rows):
    if not rows: return {"maximum_drawdown":None,"peak_date":None,"trough_date":None,"recovery_date":None,"recovery_sessions":None}
    peak=float(rows[0]["equity"]); peak_date=rows[0]["date"]; worst=0.; worst_peak=peak_date; trough=peak_date; trough_index=0
    for index,row in enumerate(rows):
        equity=float(row["equity"])
        if equity>peak: peak=equity; peak_date=row["date"]
        drawdown=equity/peak-1
        if drawdown<worst: worst=drawdown; worst_peak=peak_date; trough=row["date"]; trough_index=index
    peak_equity=next(float(row["equity"]) for row in rows if row["date"]==worst_peak)
    recovery=None; recovery_sessions=None
    for offset,row in enumerate(rows[trough_index+1:],1):
        if float(row["equity"])>=peak_equity: recovery=row["date"]; recovery_sessions=offset; break
    return {"maximum_drawdown":worst,"peak_date":worst_peak,"trough_date":trough,
            "recovery_date":recovery,"recovery_sessions":recovery_sessions}


def benchmark_scorecards(daily_rows, benchmark_rows, initial_cash, family_trials):
    benchmark_by_day={row["date"]:row for row in benchmark_rows}; summaries=[]; annual=[]; bootstraps=[]
    for strategy in sorted({row["strategy"] for row in daily_rows}):
        rows=sorted((row for row in daily_rows if row["strategy"]==strategy),key=lambda row:row["date"])
        paired=[(row,benchmark_by_day[row["date"]]) for row in rows if row["date"] in benchmark_by_day]
        values=[float(row["daily_return"]) for row in rows]; benchmark=[float(row["daily_return"]) for _,row in paired]
        total=float(rows[-1]["equity"])/initial_cash-1 if rows else 0.; spy=math.prod(1+value for value in benchmark)-1 if paired else None
        volatility=statistics.stdev(values)*math.sqrt(252) if len(values)>1 else None
        summaries.append({"strategy":strategy,"sessions":len(rows),"benchmark_sessions":len(paired),"benchmark_available":bool(paired),"portfolio_return":total,"spy_return":spy,
                          "excess_return":total-spy if spy is not None else None,"cash_excess_return":total,
                          "annualized_volatility":volatility,"total_turnover":sum(float(row["turnover"]) for row in rows),
                          "trade_count":sum(int(row["trades"]) for row in rows),**_drawdown_profile(rows),
                          **return_evidence(values,family_trials)})
        bootstrap=paired_block_bootstrap([float(row["daily_return"]) for row,_ in paired],benchmark); bootstrap["strategy"]=strategy; bootstraps.append(bootstrap)
        years=sorted({row["date"][:4] for row in rows})
        for year in years:
            candidate=[float(row["daily_return"]) for row in rows if row["date"].startswith(year)]
            bench=[float(other["daily_return"]) for row,other in paired if row["date"].startswith(year)]
            candidate_return=math.prod(1+value for value in candidate)-1; benchmark_return=math.prod(1+value for value in bench)-1 if bench else None
            annual.append({"strategy":strategy,"year":year,"sessions":len(candidate),"portfolio_return":candidate_return,
                           "spy_return":benchmark_return,"excess_return":candidate_return-benchmark_return if benchmark_return is not None else None})
    return summaries,annual,bootstraps


def cost_sensitivity(trades, initial_cash, cost_ladder=(1.,5.,10.,20.), current_cost_bps=10.):
    rows=[]
    accepted=[row for row in trades if row.get("portfolio_status")=="accepted"]
    for strategy in sorted({row["strategy"] for row in accepted}):
        group=[row for row in accepted if row["strategy"]==strategy]
        for cost in cost_ladder:
            modeled=[float(row.get("gross_return",float(row["net_return"])+2*current_cost_bps/10000))-2*cost/10000 for row in group]
            pnl=sum(float(row.get("allocated_notional") or 0)*value for row,value in zip(group,modeled))
            rows.append({"strategy":strategy,"cost_bps_per_side":cost,"accepted_trades":len(group),
                         "portfolio_return":pnl/initial_cash,"mean_trade_return":statistics.fmean(modeled) if group else None})
    return rows


def nested_regime_walk_forward(trades, session_dates, min_train_sessions, test_sessions,
                               step_sessions, initial_cash, family_trials,
                               minimum_bucket_trades=20, alpha=.05,
                               dimensions=REGIME_SELECTION_DIMENSIONS, strategies=None):
    accepted=[row for row in trades if row.get("portfolio_status")=="accepted"]
    folds=build_walk_forward_folds(sorted(set(session_dates)),min_train_sessions=min_train_sessions,
                                   test_sessions=test_sessions,step_sessions=step_sessions,expanding=True)
    rows=[]
    for fold in folds:
        for strategy in sorted(strategies or {row["strategy"] for row in accepted}):
            training=[row for row in accepted if row["strategy"]==strategy and fold.train_start<=row["date"]<=fold.train_end]
            candidates=[]
            for dimension in dimensions:
                buckets=sorted({str(row.get(dimension)) for row in training if row.get(dimension) not in (None,"")})
                for bucket in buckets:
                    values=[float(row["net_return"]) for row in training if str(row.get(dimension))==bucket]
                    if len(values)<minimum_bucket_trades: continue
                    evidence=return_evidence(values,max(family_trials,len(dimensions)*5))
                    candidates.append((statistics.fmean(values),dimension,bucket,len(values),evidence["bonferroni_adjusted_p"]))
            viable=[item for item in candidates if item[0]>0 and item[4] is not None and item[4]<=alpha]
            chosen=max(viable,default=None)
            test=[row for row in accepted if row["strategy"]==strategy and fold.test_start<=row["date"]<=fold.test_end]
            selected=[] if chosen is None else [row for row in test if str(row.get(chosen[1]))==chosen[2]]
            rows.append({"fold":fold.fold,"strategy":strategy,"train_start":fold.train_start,"train_end":fold.train_end,
                         "test_start":fold.test_start,"test_end":fold.test_end,"selection":"CASH" if chosen is None else "REGIME_BUCKET",
                         "selected_dimension":None if chosen is None else chosen[1],"selected_bucket":None if chosen is None else chosen[2],
                         "training_trades":0 if chosen is None else chosen[3],"training_mean_return":None if chosen is None else chosen[0],
                         "training_adjusted_p":None if chosen is None else chosen[4],"test_trades":len(selected),
                         "test_portfolio_return":sum(float(row.get("realized_pnl") or 0) for row in selected)/initial_cash})
    return rows
