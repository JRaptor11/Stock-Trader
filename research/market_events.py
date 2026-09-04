"""Point-in-time market-event and coverage contracts for equity research."""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

EVENT_TYPES={"HALT","LULD_PAUSE","SPLIT","DIVIDEND","DELISTING"}


def _bool(value, field, row_number):
    value=str(value or "").strip().lower()
    if value not in {"true","false","1","0"}:
        raise ValueError(f"market events row {row_number} {field} must be boolean")
    return value in {"true","1"}


def load_market_events(path: Path) -> tuple[list[dict],dict[str,dict]]:
    required={"record_type","symbol","effective_date","event_type","start_timestamp","end_timestamp","delisting_return","halt_luld_complete","corporate_actions_complete","delistings_complete","source"}
    with path.open("r",newline="",encoding="utf-8-sig") as handle:
        reader=csv.DictReader(handle)
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError(f"market events missing columns: {sorted(required-set(reader.fieldnames or ())) }")
        events=[]; coverage={}
        for number,row in enumerate(reader,start=2):
            kind=str(row["record_type"]).strip().lower(); effective=date.fromisoformat(row["effective_date"]).isoformat()
            if not str(row["source"]).strip(): raise ValueError(f"market events row {number} has no source")
            if kind=="coverage":
                if effective in coverage: raise ValueError(f"duplicate market-event coverage date: {effective}")
                coverage[effective]={"halt_luld_complete":_bool(row["halt_luld_complete"],"halt_luld_complete",number),"corporate_actions_complete":_bool(row["corporate_actions_complete"],"corporate_actions_complete",number),"delistings_complete":_bool(row["delistings_complete"],"delistings_complete",number),"source":row["source"]}
                continue
            if kind!="event": raise ValueError(f"market events row {number} has unknown record_type")
            symbol=str(row["symbol"]).strip().upper(); event_type=str(row["event_type"]).strip().upper()
            if not symbol or event_type not in EVENT_TYPES: raise ValueError(f"market events row {number} has invalid event")
            start=datetime.fromisoformat(row["start_timestamp"].replace("Z","+00:00")) if row["start_timestamp"] else None
            end=datetime.fromisoformat(row["end_timestamp"].replace("Z","+00:00")) if row["end_timestamp"] else None
            if (start and start.tzinfo is None) or (end and end.tzinfo is None) or (start and end and end<start): raise ValueError(f"market events row {number} has invalid timestamps")
            if event_type in {"HALT","LULD_PAUSE"} and (not start or not end): raise ValueError(f"market events row {number} halt/LULD requires timestamps")
            if start and start.date().isoformat()!=effective: raise ValueError(f"market events row {number} timestamp differs from effective_date")
            delisting=float(row["delisting_return"]) if row["delisting_return"] else None
            if event_type=="DELISTING" and delisting is None: raise ValueError(f"market events row {number} delisting requires a return")
            if delisting is not None and delisting < -1: raise ValueError(f"market events row {number} delisting return cannot be below -100%")
            events.append({**row,"symbol":symbol,"effective_date":effective,"event_type":event_type,"start_timestamp":start.isoformat() if start else "","end_timestamp":end.isoformat() if end else "","delisting_return":delisting})
    return events,coverage


def market_event_audit(session_dates, events, coverage):
    dates=sorted(set(session_dates)); missing={name:[day for day in dates if not coverage.get(day,{}).get(name)] for name in ("halt_luld_complete","corporate_actions_complete","delistings_complete")}
    return {"session_count":len(dates),"event_count":len(events),"halt_luld_events":sum(row["event_type"] in {"HALT","LULD_PAUSE"} for row in events),"corporate_action_events":sum(row["event_type"] in {"SPLIT","DIVIDEND"} for row in events),"delisting_events":sum(row["event_type"]=="DELISTING" for row in events),"halt_luld_complete":not missing["halt_luld_complete"] and bool(dates),"corporate_actions_complete":not missing["corporate_actions_complete"] and bool(dates),"delistings_complete":not missing["delistings_complete"] and bool(dates),"missing_coverage_dates":missing}


def blocked_event(symbol, timestamp, events):
    instant=datetime.fromisoformat(str(timestamp).replace("Z","+00:00"))
    for row in events:
        if row["symbol"]!=symbol or row["event_type"] not in {"HALT","LULD_PAUSE"}: continue
        start=datetime.fromisoformat(row["start_timestamp"]); end=datetime.fromisoformat(row["end_timestamp"])
        if start<=instant<=end: return row["event_type"]
    return None
