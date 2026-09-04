"""Matched controls and chronological stability diagnostics for intraday trades."""
import math, statistics
from research.execution_model import entry_fill, exit_fill, net_return
from research.walk_forward import build_walk_forward_folds

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

def walk_forward_trade_scorecards(trades, min_train_sessions=252, test_sessions=63, step_sessions=63, initial_cash=100_000.0):
    accepted=[r for r in trades if r.get("portfolio_status")=="accepted"]; dates=sorted({r["date"] for r in accepted})
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
