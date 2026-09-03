"""Validation-only Yahoo chart downloader normalized to replay CSV format.

Yahoo's chart endpoint is not a contracted production feed. It is used solely
to detect whether conclusions replicate away from Alpaca/IEX and may change or
become unavailable without notice.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


FIELDS=("timestamp","symbol","open","high","low","close","volume","trade_count","vwap")


def download(symbols: list[str], start: str, end: str, output: Path) -> int:
    period1=int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    period2=int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())
    rows=[]
    for symbol in symbols:
        params=urllib.parse.urlencode({"period1":period1,"period2":period2,"interval":"1d","events":"history","includeAdjustedClose":"true"})
        request=urllib.request.Request(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{params}",headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(request,timeout=60) as response: result=json.load(response)["chart"]["result"][0]
        quote=result["indicators"]["quote"][0]; adjusted=result["indicators"].get("adjclose",[{}])[0].get("adjclose",quote["close"])
        for i,stamp in enumerate(result["timestamp"]):
            raw_close=quote["close"][i]
            if raw_close is None or adjusted[i] is None: continue
            factor=adjusted[i]/raw_close if raw_close else 1.0
            if any(quote[key][i] is None for key in ("open","high","low")): continue
            rows.append({"timestamp":datetime.fromtimestamp(stamp,timezone.utc).isoformat(),"symbol":symbol,
                         "open":quote["open"][i]*factor,"high":quote["high"][i]*factor,"low":quote["low"][i]*factor,"close":adjusted[i],
                         "volume":quote["volume"][i] or 0,"trade_count":0,"vwap":adjusted[i]})
        time.sleep(.1)
    rows.sort(key=lambda r:(r["timestamp"],r["symbol"])); output.parent.mkdir(parents=True,exist_ok=True)
    with output.open("w",newline="",encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=FIELDS); writer.writeheader(); writer.writerows(rows)
    return len(rows)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--symbols",required=True); parser.add_argument("--start",required=True); parser.add_argument("--end",required=True); parser.add_argument("--output",type=Path,required=True); args=parser.parse_args()
    print(json.dumps({"rows":download([s.strip().upper() for s in args.symbols.split(',') if s.strip()],args.start,args.end,args.output),"output":str(args.output),"source":"Yahoo chart endpoint","use":"validation_only"}))


if __name__ == "__main__": main()
