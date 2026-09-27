"""Submit one completed daily session to the broker-free shadow coordinator."""

from __future__ import annotations

import argparse
import csv
import json
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from research.market_conditions import causal_market_conditions
from research.market_state_episodes import causal_state_labels
from research.universes import resolve_universe


def _token(path: Path) -> str:
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("RESEARCH_API_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    raise ValueError("RESEARCH_API_TOKEN missing")


def _env_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def fetch_first_iex_opening_quotes(
    *, session: str, symbols: list[str], alpaca_env: Path,
) -> tuple[dict, dict]:
    """Fetch one first IEX quote per symbol after 09:30 ET, diagnostics only."""
    credentials = _env_values(alpaca_env)
    key = credentials.get("API_KEY") or credentials.get("APCA_API_KEY_ID")
    secret = credentials.get("SECRET_KEY") or credentials.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise ValueError("Alpaca API_KEY and SECRET_KEY are required for quote diagnostics")
    eastern = ZoneInfo("America/New_York")
    day = datetime.strptime(session, "%Y-%m-%d").date()
    start = datetime.combine(day, time(9, 30), eastern)
    end = start + timedelta(minutes=1)
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    quotes, failures = {}, {}
    for symbol in sorted(set(symbols)):
        query = urllib.parse.urlencode({
            "start": start.isoformat(), "end": end.isoformat(),
            "feed": "iex", "sort": "asc", "limit": 1,
        })
        request = urllib.request.Request(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes?{query}",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                document = json.load(response)
            rows = document.get("quotes") or []
            if not rows:
                failures[symbol] = "no_iex_quote_in_first_minute"
                continue
            row = rows[0]
            quotes[symbol] = {
                "bid": float(row["bp"]), "ask": float(row["ap"]),
                "observed_at": str(row["t"]), "feed": "iex",
            }
        except Exception as exc:  # diagnostics must not block the frozen test
            failures[symbol] = type(exc).__name__
    metadata = {
        "requested_symbols": len(set(symbols)), "captured_symbols": len(quotes),
        "failed_symbols": failures, "window_start": start.isoformat(),
        "window_end": end.isoformat(), "used_for_execution": False,
    }
    return quotes, metadata


def build_payload(csv_path: Path, next_session: str, code_revision: str) -> dict:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    by_symbol = defaultdict(list)
    for row in rows: by_symbol[row["symbol"]].append(row)
    latest = max(row["timestamp"][:10] for row in rows)
    histories, bars = {}, {}
    for symbol, values in by_symbol.items():
        values.sort(key=lambda row: row["timestamp"])
        completed = [row for row in values if row["timestamp"][:10] <= latest][-400:]
        histories[symbol] = [float(row["close"]) for row in completed[:-1]]
        row = completed[-1]
        bars[symbol] = {
            key: float(row[key]) for key in ("open", "high", "low", "close", "volume")
        }
    universe = resolve_universe("ETF_LONG_TERM_RESEARCH_EXPANDED")
    by_date = defaultdict(dict)
    for row in rows:
        by_date[row["timestamp"][:10]][row["symbol"]] = {
            key: float(row[key]) for key in ("open", "high", "low", "close", "volume")
        }
    dates = sorted(day for day, values in by_date.items()
                   if all(symbol in values for symbol in universe))
    conditions = causal_market_conditions(dates, by_date, universe)
    labels = causal_state_labels(conditions)
    latest_condition = conditions[latest]
    latest_label = labels[latest]
    bootstrap_conditions = {day: conditions[day] for day in sorted(conditions)[-10:]}
    return {"session": latest, "next_session": next_session,
            "market_bar_timestamp": max(row["timestamp"] for row in rows),
            "source_observed_at": datetime.now(timezone.utc).isoformat(),
            "code_revision": code_revision, "data_source": "alpaca_iex_adjusted_1d",
            "bootstrap_histories": histories, "bars": bars,
            "state_evidence": latest_condition,
            "bootstrap_state_evidence": bootstrap_conditions,
            "raw_state": latest_label["raw_core_state"],
            "confirmed_state": latest_label["core_state"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--next-session", required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--token-env", required=True, type=Path)
    parser.add_argument(
        "--opening-quotes-json", type=Path,
        help="Optional diagnostic first-quote evidence; never used for fills.",
    )
    parser.add_argument(
        "--alpaca-env", type=Path,
        help="Optionally fetch the first historical IEX quote after 09:30 ET.",
    )
    parser.add_argument("--url", default="https://stock-trader-9du8.onrender.com")
    args = parser.parse_args()
    token = _token(args.token_env)
    status_request = urllib.request.Request(
        args.url.rstrip("/") + "/api/shadow/status",
        headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(status_request, timeout=120) as response:
        status = json.load(response)
    document = build_payload(args.csv, args.next_session, args.code_revision)
    if args.opening_quotes_json:
        document["opening_quote_diagnostics"] = json.loads(
            args.opening_quotes_json.read_text(encoding="utf-8")
        )
    elif args.alpaca_env:
        quotes, metadata = fetch_first_iex_opening_quotes(
            session=document["session"], symbols=list(document["bars"]),
            alpaca_env=args.alpaca_env,
        )
        document["opening_quote_diagnostics"] = quotes
        document["opening_quote_collection"] = metadata
    if status.get("last_session") is not None:
        document.pop("bootstrap_histories", None)
        if (status.get("market_state") or {}).get("active"):
            document.pop("bootstrap_state_evidence", None)
    payload = json.dumps(document).encode()
    request = urllib.request.Request(
        args.url.rstrip("/") + "/api/shadow/sessions", data=payload, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        print(json.dumps(json.load(response), indent=2))


if __name__ == "__main__": main()
