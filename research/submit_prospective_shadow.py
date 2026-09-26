"""Submit one completed daily session to the broker-free shadow coordinator."""

from __future__ import annotations

import argparse
import csv
import json
import urllib.request
from collections import defaultdict
from pathlib import Path


def _token(path: Path) -> str:
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("RESEARCH_API_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    raise ValueError("RESEARCH_API_TOKEN missing")


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
        bars[symbol] = {key: float(row[key]) for key in ("open", "close", "volume")}
    return {"session": latest, "next_session": next_session,
            "source_observed_at": max(row["timestamp"] for row in rows),
            "code_revision": code_revision, "data_source": "alpaca_iex_adjusted_1d",
            "bootstrap_histories": histories, "bars": bars}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--next-session", required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--token-env", required=True, type=Path)
    parser.add_argument("--url", default="https://stock-trader-9du8.onrender.com")
    args = parser.parse_args()
    payload = json.dumps(build_payload(args.csv, args.next_session,
                                       args.code_revision)).encode()
    request = urllib.request.Request(
        args.url.rstrip("/") + "/api/shadow/sessions", data=payload, method="POST",
        headers={"Authorization": "Bearer " + _token(args.token_env),
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        print(json.dumps(json.load(response), indent=2))


if __name__ == "__main__": main()
