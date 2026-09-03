"""Download long-form Alpaca bars for an offline research dataset."""

from __future__ import annotations

import argparse
import csv
import json
import urllib.parse
import urllib.request
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


def download(*, symbols: list[str], start: str, end: str, feed: str,
             timeframe: str = "5Min",
             api_key: str, secret_key: str, output: Path) -> tuple[int, int]:
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    rows: list[dict] = []
    page_token: str | None = None
    pages = 0
    while True:
        params = {
            "symbols": ",".join(symbols), "timeframe": timeframe, "start": start,
            "end": end, "adjustment": "all", "feed": feed, "limit": "10000",
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
                rows.append({
                    "timestamp": bar["t"], "symbol": symbol,
                    "open": bar["o"], "high": bar["h"], "low": bar["l"],
                    "close": bar["c"], "volume": bar["v"],
                    "trade_count": bar.get("n", 0), "vwap": bar.get("vw", 0),
                })
        page_token = payload.get("next_page_token")
        if not page_token:
            break
    rows.sort(key=lambda row: (row["timestamp"], row["symbol"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows), pages


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--feed", default="iex")
    parser.add_argument("--timeframe", default="5Min", choices=("5Min", "1Day"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env = _env_file(args.env_file)
    rows, pages = download(
        symbols=[item.strip().upper() for item in args.symbols.split(",") if item.strip()],
        start=args.start, end=args.end, feed=args.feed, timeframe=args.timeframe,
        api_key=env["API_KEY"], secret_key=env["SECRET_KEY"], output=args.output,
    )
    print(json.dumps({"rows": rows, "pages": pages, "output": str(args.output)}))


if __name__ == "__main__":
    main()
