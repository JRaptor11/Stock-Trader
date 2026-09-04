"""Download long-form Alpaca bars for an offline research dataset."""

from __future__ import annotations

import argparse
import csv
import json
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


FIELDS = ("timestamp", "symbol", "open", "high", "low", "close", "volume", "trade_count", "vwap")


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def date_chunks(start: str, end: str, chunk_days: int) -> list[tuple[str, str]]:
    """Return non-overlapping half-open date ranges for deterministic downloads."""
    if chunk_days < 1:
        raise ValueError("chunk_days must be positive")
    cursor, finish = _date(start), _date(end)
    if cursor >= finish:
        raise ValueError("start must precede end")
    chunks = []
    while cursor < finish:
        boundary = min(cursor + timedelta(days=chunk_days), finish)
        chunks.append((cursor.isoformat(), boundary.isoformat()))
        cursor = boundary
    return chunks


def download(*, symbols: list[str], start: str, end: str, feed: str,
             timeframe: str = "5Min", chunk_days: int | None = None,
             api_key: str, secret_key: str, output: Path) -> dict:
    """Download each symbol and date chunk independently, deduplicating boundaries."""
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    rows: dict[tuple[str, str], dict] = {}
    pages = 0
    ranges = date_chunks(start, end, chunk_days or (366 if timeframe == "1Day" else 31))
    for requested_symbol in symbols:
        for chunk_start, chunk_end in ranges:
            page_token: str | None = None
            while True:
                params = {
                    "symbols": requested_symbol, "timeframe": timeframe,
                    "start": chunk_start, "end": chunk_end,
                    "adjustment": "all", "feed": feed, "limit": "10000",
                    "sort": "asc",
                }
                if page_token:
                    params["page_token"] = page_token
                url = "https://data.alpaca.markets/v2/stocks/bars?" + urllib.parse.urlencode(params)
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=90) as response:
                    payload = json.load(response)
                pages += 1
                for symbol, bars in payload.get("bars", {}).items():
                    for bar in bars:
                        row = {
                            "timestamp": bar["t"], "symbol": symbol,
                            "open": bar["o"], "high": bar["h"], "low": bar["l"],
                            "close": bar["c"], "volume": bar["v"],
                            "trade_count": bar.get("n", 0), "vwap": bar.get("vw", 0),
                        }
                        rows[(symbol, bar["t"])] = row
                page_token = payload.get("next_page_token")
                if not page_token:
                    break
    ordered = sorted(rows.values(), key=lambda row: (row["timestamp"], row["symbol"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(ordered)
    per_symbol = {}
    for symbol in symbols:
        selected = [row["timestamp"] for row in ordered if row["symbol"] == symbol]
        per_symbol[symbol] = {
            "rows": len(selected),
            "first": min(selected, default=None),
            "last": max(selected, default=None),
        }
    return {
        "rows": len(ordered), "pages": pages, "chunks_per_symbol": len(ranges),
        "requested_start": start, "requested_end": end,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "per_symbol": per_symbol,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--feed", default="iex")
    parser.add_argument("--timeframe", default="5Min", choices=("5Min", "1Day"))
    parser.add_argument("--chunk-days", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env = _env_file(args.env_file)
    result = download(
        symbols=[item.strip().upper() for item in args.symbols.split(",") if item.strip()],
        start=args.start, end=args.end, feed=args.feed, timeframe=args.timeframe,
        chunk_days=args.chunk_days, api_key=env["API_KEY"],
        secret_key=env["SECRET_KEY"], output=args.output,
    )
    result["output"] = str(args.output)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
