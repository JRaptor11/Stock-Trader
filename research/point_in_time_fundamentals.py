"""Causal market-cap and float snapshots for historical equity research."""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path


def load_fundamentals(path: Path) -> dict[str,list[dict]]:
    required={"symbol","effective_date","known_at","market_cap","float_shares","source"}; by_symbol={}; seen=set()
    with path.open("r",newline="",encoding="utf-8-sig") as handle:
        reader=csv.DictReader(handle)
        if not required.issubset(reader.fieldnames or ()): raise ValueError(f"fundamentals missing columns: {sorted(required-set(reader.fieldnames or ())) }")
        for number,row in enumerate(reader,start=2):
            symbol=str(row["symbol"]).strip().upper(); effective=date.fromisoformat(row["effective_date"]); known=datetime.fromisoformat(row["known_at"].replace("Z","+00:00"))
            if not symbol or known.tzinfo is None or not str(row["source"]).strip(): raise ValueError(f"fundamentals row {number} has invalid identity or provenance")
            market_cap=float(row["market_cap"]); float_shares=float(row["float_shares"])
            if market_cap<=0 or float_shares<=0: raise ValueError(f"fundamentals row {number} values must be positive")
            key=(symbol,effective.isoformat(),known.isoformat())
            if key in seen: raise ValueError(f"duplicate fundamentals snapshot: {key}")
            seen.add(key); by_symbol.setdefault(symbol,[]).append({"symbol":symbol,"effective_date":effective.isoformat(),"known_at":known.isoformat(),"market_cap":market_cap,"float_shares":float_shares,"source":row["source"]})
    for rows in by_symbol.values(): rows.sort(key=lambda row:(datetime.fromisoformat(row["known_at"]),row["effective_date"]))
    return by_symbol


def snapshot_at(by_symbol, symbol, timestamp):
    instant=datetime.fromisoformat(str(timestamp).replace("Z","+00:00")); day=instant.date().isoformat(); eligible=[]
    for row in by_symbol.get(symbol,()):
        if datetime.fromisoformat(row["known_at"])<=instant and row["effective_date"]<=day: eligible.append(row)
    return eligible[-1] if eligible else None


def fundamentals_audit(sessions, by_symbol):
    diagnostics=[]; covered=market_cap=float_count=0; observed=0
    for day,symbols in sorted(sessions.items()):
        for symbol,bars in sorted(symbols.items()):
            observed+=1; row=snapshot_at(by_symbol,symbol,bars[0]["timestamp"]); covered+=row is not None
            if row: market_cap+=1; float_count+=1
            diagnostics.append({"date":day,"symbol":symbol,"entry_time_snapshot_available":row is not None,"known_at":row["known_at"] if row else None,"effective_date":row["effective_date"] if row else None,"market_cap":row["market_cap"] if row else None,"float_shares":row["float_shares"] if row else None,"source":row["source"] if row else None})
    return {"observed_symbol_sessions":observed,"covered_symbol_sessions":covered,"coverage":covered/observed if observed else 0.,"market_cap_complete":market_cap==observed and observed>0,"float_complete":float_count==observed and observed>0},diagnostics
