"""Shared data, experiment, and promotion contracts for research engines."""
from __future__ import annotations
import csv, hashlib, json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

@dataclass(frozen=True)
class DatasetAudit:
    sha256: str; rows: int; symbols: int; first_timestamp: str | None; last_timestamp: str | None
    duplicate_symbol_timestamps: int; missing_ohlcv_rows: int; feed: str; adjusted: bool

def audit_bar_csv(path: Path, *, feed="unknown", adjusted=False) -> DatasetAudit:
    digest=hashlib.sha256(); seen=set(); duplicates=missing=rows=0; symbols=set(); first=last=None
    with path.open("rb") as raw:
        for chunk in iter(lambda:raw.read(1024*1024),b""): digest.update(chunk)
    with path.open("r",newline="",encoding="utf-8-sig") as handle:
        reader=csv.DictReader(handle); required={"timestamp","symbol","open","high","low","close","volume"}
        if not required.issubset(reader.fieldnames or ()): raise ValueError(f"bar CSV missing columns: {sorted(required-set(reader.fieldnames or ())) }")
        for row in reader:
            rows+=1; key=(row["timestamp"],row["symbol"].upper()); duplicates+=key in seen; seen.add(key); symbols.add(key[1])
            missing+=any(row.get(name) in (None,"") for name in ("open","high","low","close","volume")); first=min(first,row["timestamp"]) if first else row["timestamp"]; last=max(last,row["timestamp"]) if last else row["timestamp"]
    return DatasetAudit(digest.hexdigest(),rows,len(symbols),first,last,duplicates,missing,feed,adjusted)

def _boolean(value, field):
    normalized=str(value).strip().lower()
    if normalized not in {"true","false","1","0"}: raise ValueError(f"security master {field} must be boolean")
    return normalized in {"true","1"}

def load_security_master(path: Path) -> list[dict]:
    required={"symbol","effective_from","effective_to","listed","tradable","exchange","security_type"}
    with path.open("r",newline="",encoding="utf-8-sig") as handle:
        reader=csv.DictReader(handle)
        if not required.issubset(reader.fieldnames or ()): raise ValueError(f"security master missing columns: {sorted(required-set(reader.fieldnames or ())) }")
        rows=[]
        for number,row in enumerate(reader,start=2):
            symbol=str(row["symbol"]).strip().upper()
            if not symbol: raise ValueError(f"security master row {number} has no symbol")
            start=date.fromisoformat(row["effective_from"]); end=date.fromisoformat(row["effective_to"]) if row["effective_to"] else None
            if end and end < start: raise ValueError(f"security master row {number} ends before it starts")
            rows.append({**row,"symbol":symbol,"effective_from":start.isoformat(),"effective_to":end.isoformat() if end else "","listed":_boolean(row["listed"],"listed"),"tradable":_boolean(row["tradable"],"tradable")})
    by_symbol={}
    for row in sorted(rows,key=lambda item:(item["symbol"],item["effective_from"])):
        previous=by_symbol.get(row["symbol"])
        if previous and (not previous["effective_to"] or row["effective_from"]<=previous["effective_to"]):
            raise ValueError(f"security master has overlapping intervals for {row['symbol']}")
        by_symbol[row["symbol"]]=row
    return rows

def apply_security_master(sessions: dict, rows: list[dict]) -> tuple[dict,dict,list[dict]]:
    """Causally filter symbol-sessions and report classification coverage."""
    intervals={}
    for row in rows: intervals.setdefault(row["symbol"],[]).append(row)
    filtered={}; diagnostics=[]; observed=classified=eligible=0
    for day,symbols in sorted(sessions.items()):
        kept={}
        for symbol,bars in sorted(symbols.items()):
            observed+=1; matches=[row for row in intervals.get(symbol,()) if row["effective_from"]<=day and (not row["effective_to"] or day<=row["effective_to"])]
            row=matches[0] if matches else None; classified+=row is not None
            allowed=bool(row and row["listed"] and row["tradable"]); eligible+=allowed
            reason="eligible" if allowed else ("not_listed_or_tradable" if row else "no_effective_record")
            diagnostics.append({"date":day,"symbol":symbol,"classified":row is not None,"eligible":allowed,"reason":reason,"exchange":row["exchange"] if row else None,"security_type":row["security_type"] if row else None})
            if allowed: kept[symbol]=bars
        if kept: filtered[day]=kept
    summary={"observed_symbol_sessions":observed,"classified_symbol_sessions":classified,"eligible_symbol_sessions":eligible,"excluded_symbol_sessions":observed-eligible,"classification_coverage":classified/observed if observed else 0.,"complete_observed_coverage":classified==observed and observed>0}
    return filtered,summary,diagnostics

def promotion_gate(*, audit: DatasetAudit, security_master: bool, halt_luld: bool,
                   point_in_time_cap: bool, point_in_time_float: bool, minimum_trades_met: bool,
                   corporate_actions: bool=True, delistings: bool=True) -> dict:
    checks={"no_duplicate_bars":audit.duplicate_symbol_timestamps==0,"complete_ohlcv":audit.missing_ohlcv_rows==0,
            "point_in_time_security_master":security_master,"halt_luld_available":halt_luld,
            "point_in_time_market_cap":point_in_time_cap,"point_in_time_float":point_in_time_float,
            "corporate_actions_complete":corporate_actions,"delistings_complete":delistings,
            "minimum_trades_met":minimum_trades_met}
    return {"checks":checks,"promotable":all(checks.values()),"failed_checks":[k for k,v in checks.items() if not v]}

def write_dataset_manifest(path: Path, audit: DatasetAudit, metadata: dict) -> None:
    path.write_text(json.dumps({"schema_version":1,"audit":asdict(audit),"metadata":metadata},indent=2,sort_keys=True),encoding="utf-8")
