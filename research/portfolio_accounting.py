"""Capital-constrained accounting for independently evaluated trade streams."""
from __future__ import annotations
from collections import defaultdict

def allocate_trades(trades: list[dict], *, initial_cash: float, target_notional: float,
                    maximum_positions: int, maximum_symbol_pct: float) -> tuple[list[dict], list[dict]]:
    """Reserve capital at entry and realize it at exit for each strategy separately."""
    results=[]; curves=[]
    for strategy in sorted({row["strategy"] for row in trades}):
        cash=float(initial_cash); active=[]; realized=0.; peak=initial_cash
        ordered=sorted((dict(row) for row in trades if row["strategy"]==strategy),key=lambda r:(r["entry_timestamp"],r["symbol"]))
        for trade in ordered:
            still=[]
            for position in active:
                if position["exit_timestamp"] <= trade["entry_timestamp"]:
                    pnl=position["allocated_notional"]*position["net_return"]; cash+=position["allocated_notional"]+pnl; realized+=pnl
                    peak=max(peak,cash+sum(p["allocated_notional"] for p in still))
                    curves.append({"strategy":strategy,"timestamp":position["exit_timestamp"],"event":"exit","cash":cash,"realized_pnl":realized,"active_positions":len(still)})
                else: still.append(position)
            active=still; symbol_reserved=sum(p["allocated_notional"] for p in active if p["symbol"]==trade["symbol"])
            cap=initial_cash*maximum_symbol_pct
            reason=None
            if len(active)>=maximum_positions: reason="maximum_positions"
            elif cash<target_notional: reason="insufficient_cash"
            elif symbol_reserved+target_notional>cap: reason="symbol_exposure_limit"
            if reason:
                trade.update({"portfolio_status":"rejected","portfolio_rejection_reason":reason,"allocated_notional":0.,"realized_pnl":0.})
            else:
                cash-=target_notional; trade.update({"portfolio_status":"accepted","portfolio_rejection_reason":"","allocated_notional":target_notional,"realized_pnl":target_notional*trade["net_return"]}); active.append(trade)
                curves.append({"strategy":strategy,"timestamp":trade["entry_timestamp"],"event":"entry","cash":cash,"realized_pnl":realized,"active_positions":len(active)})
            results.append(trade)
        for position in sorted(active,key=lambda r:r["exit_timestamp"]):
            pnl=position["allocated_notional"]*position["net_return"]; cash+=position["allocated_notional"]+pnl; realized+=pnl
            curves.append({"strategy":strategy,"timestamp":position["exit_timestamp"],"event":"exit","cash":cash,"realized_pnl":realized,"active_positions":0})
    return results,sorted(curves,key=lambda r:(r["strategy"],r["timestamp"],r["event"]))
