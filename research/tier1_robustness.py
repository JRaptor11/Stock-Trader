"""Post-selection robustness diagnostics for a frozen Tier 1 result.

This module analyzes an already-selected result archive. It does not search
parameters or promote post-hoc benchmarks as candidate strategies.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import statistics
import zipfile
from collections import defaultdict
from pathlib import Path

from research.tier1_etf_replay import SECTOR_ETFS, _metrics, load_daily_bars


def _zip_rows(bundle: zipfile.ZipFile, name: str) -> list[dict]:
    with bundle.open(name) as raw:
        return list(csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8")))


def _series(rows: list[dict], strategy: str, cost_bps: float) -> list[dict]:
    selected = [r for r in rows if r["strategy"] == strategy and float(r["cost_bps"]) == cost_bps]
    selected.sort(key=lambda r: r["date"])
    prior = None; result = []
    for row in selected:
        equity = float(row["equity"])
        result.append({"date": row["date"], "equity": equity,
                       "return": equity / prior - 1.0 if prior else 0.0})
        prior = equity
    return result


def _compound(values):
    result = 1.0
    for value in values: result *= 1.0 + value
    return result - 1.0


def rolling_comparison(candidate: list[dict], benchmark: list[dict], start: str,
                       windows=(63, 126, 252), step=21) -> list[dict]:
    cand = {r["date"]: r["return"] for r in candidate if r["date"] >= start}
    bench = {r["date"]: r["return"] for r in benchmark if r["date"] >= start}
    dates = sorted(set(cand) & set(bench)); rows = []
    for window in windows:
        for end in range(window, len(dates) + 1, step):
            sample = dates[end-window:end]
            cr = _compound(cand[d] for d in sample); br = _compound(bench[d] for d in sample)
            rows.append({"window_sessions": window, "start": sample[0], "end": sample[-1],
                         "candidate_return": cr, "benchmark_return": br,
                         "excess_return": cr-br, "candidate_won": cr > br})
    return rows


def block_bootstrap(candidate: list[dict], benchmark: list[dict], start: str,
                    samples=5000, block=20, seed=20260903) -> dict:
    cand={r["date"]:r["return"] for r in candidate if r["date"]>=start}; bench={r["date"]:r["return"] for r in benchmark if r["date"]>=start}
    dates=sorted(set(cand)&set(bench)); pairs=[(cand[d],bench[d]) for d in dates]
    rng=random.Random(seed); excess=[]; length=len(pairs)
    for _ in range(samples):
        draw=[]
        while len(draw)<length:
            begin=rng.randrange(0,max(1,length-block+1)); draw.extend(pairs[begin:begin+block])
        draw=draw[:length]; excess.append(_compound(x[0] for x in draw)-_compound(x[1] for x in draw))
    excess.sort()
    def quantile(p): return excess[min(len(excess)-1,max(0,int(p*(len(excess)-1))))]
    return {"method":"paired moving-block bootstrap","samples":samples,"block_sessions":block,"seed":seed,
            "observations":length,"observed_excess_return":_compound(x[0] for x in pairs)-_compound(x[1] for x in pairs),
            "probability_excess_positive":sum(x>0 for x in excess)/len(excess),
            "excess_return_ci_95":[quantile(.025),quantile(.975)]}


def annual_comparison(candidate: list[dict], benchmark: list[dict], start: str) -> list[dict]:
    """Calendar-year results expose whether excess return comes from one episode."""
    cand = {row["date"]: row["return"] for row in candidate if row["date"] >= start}
    bench = {row["date"]: row["return"] for row in benchmark if row["date"] >= start}
    grouped = defaultdict(list)
    for day in sorted(set(cand) & set(bench)):
        grouped[day[:4]].append((cand[day], bench[day]))
    rows = []
    for year, pairs in sorted(grouped.items()):
        candidate_return = _compound(pair[0] for pair in pairs)
        benchmark_return = _compound(pair[1] for pair in pairs)
        rows.append({
            "year": year, "sessions": len(pairs),
            "candidate_return": candidate_return,
            "benchmark_return": benchmark_return,
            "excess_return": candidate_return - benchmark_return,
            "candidate_won": candidate_return > benchmark_return,
        })
    return rows


def drawdown_profile(series: list[dict]) -> dict:
    """Return maximum drawdown dates and recovery time, not only its magnitude."""
    if not series:
        return {"max_drawdown": None, "peak_date": None, "trough_date": None,
                "recovery_date": None, "recovery_sessions": None}
    peak_equity = series[0]["equity"]
    peak_date = series[0]["date"]
    worst = 0.0
    worst_peak_date = peak_date
    trough_date = peak_date
    trough_index = 0
    recovery_date = None
    for index, row in enumerate(series):
        if row["equity"] > peak_equity:
            peak_equity = row["equity"]
            peak_date = row["date"]
        drawdown = row["equity"] / peak_equity - 1.0
        if drawdown < worst:
            worst = drawdown
            worst_peak_date = peak_date
            trough_date = row["date"]
            trough_index = index
    peak_value = next(row["equity"] for row in series if row["date"] == worst_peak_date)
    for row in series[trough_index + 1:]:
        if row["equity"] >= peak_value:
            recovery_date = row["date"]
            break
    recovery_sessions = None
    if recovery_date:
        recovery_sessions = next(
            index for index, row in enumerate(series[trough_index + 1:], 1)
            if row["date"] == recovery_date
        )
    return {"max_drawdown": worst, "peak_date": worst_peak_date,
            "trough_date": trough_date, "recovery_date": recovery_date,
            "recovery_sessions": recovery_sessions}


def regime_attribution(candidate: list[dict], benchmark: list[dict], bars: dict,
                       dates: list[str], discovery_end: str, holdout_start: str) -> tuple[list[dict], dict]:
    closes=[]; features={}
    for day in dates:
        if "SPY" not in bars[day]: continue
        closes.append(float(bars[day]["SPY"]["close"]))
        if len(closes)>60:
            returns=[closes[i]/closes[i-1]-1 for i in range(len(closes)-59,len(closes))]
            features[day]={"above_200d":len(closes)>=200 and closes[-1]>statistics.fmean(closes[-200:]),
                           "volatility":statistics.stdev(returns)*math.sqrt(252)}
    threshold=statistics.median(v["volatility"] for d,v in features.items() if d<=discovery_end)
    cand={r["date"]:r["return"] for r in candidate}; bench={r["date"]:r["return"] for r in benchmark}; grouped=defaultdict(list)
    for day in sorted(set(cand)&set(bench)&set(features)):
        if day<holdout_start: continue
        state=("BULL" if features[day]["above_200d"] else "BEAR")+("_HIGH_VOL" if features[day]["volatility"]>threshold else "_LOW_VOL")
        grouped[state].append((cand[day],bench[day]))
    rows=[]
    for state,pairs in sorted(grouped.items()):
        cr=_compound(x[0] for x in pairs); br=_compound(x[1] for x in pairs)
        rows.append({"regime":state,"sessions":len(pairs),"candidate_return":cr,"benchmark_return":br,"excess_return":cr-br})
    return rows,{"volatility_threshold_annualized":threshold,"threshold_fit_period_end":discovery_end,"trend_definition":"SPY close above trailing 200-session mean"}


def holding_attribution(trades: list[dict], bars: dict, dates: list[str], strategy: str,
                        cost_bps: float, start: str) -> list[dict]:
    selected=defaultdict(list)
    for row in trades:
        if row["strategy"]==strategy and float(row["cost_bps"])==cost_bps: selected[row["date"]].append(row)
    shares=defaultdict(float); previous={}; totals=defaultdict(lambda:{"overnight_pnl":0.0,"intraday_pnl":0.0,"cost":0.0,"trades":0})
    for day in dates:
        today=bars[day]
        if day>=start:
            for symbol,quantity in list(shares.items()):
                if symbol in today and symbol in previous: totals[symbol]["overnight_pnl"] += quantity*(today[symbol]["open"]-previous[symbol])
        for trade in selected.get(day,[]):
            symbol=trade["symbol"]; shares[symbol]+=float(trade["notional"])/today[symbol]["open"]
            if day>=start: totals[symbol]["cost"]+=float(trade["cost"]); totals[symbol]["trades"]+=1
        if day>=start:
            for symbol,quantity in list(shares.items()):
                if symbol in today: totals[symbol]["intraday_pnl"] += quantity*(today[symbol]["close"]-today[symbol]["open"])
        previous={symbol:float(row["close"]) for symbol,row in today.items()}
    rows=[]
    for symbol,value in totals.items():
        net=value["overnight_pnl"]+value["intraday_pnl"]-value["cost"]
        rows.append({"symbol":symbol,**value,"net_contribution":net})
    return sorted(rows,key=lambda r:r["net_contribution"],reverse=True)


def simple_benchmarks(bars: dict, dates: list[str], start: str, cost_bps: float) -> list[dict]:
    evaluation=[d for d in dates if d>=start and all(s in bars[d] for s in SECTOR_ETFS)]
    if not evaluation: return []
    first=evaluation[0]; last=evaluation[-1]; results=[]
    returns=[]
    for symbol in SECTOR_ETFS:
        returns.append(bars[last][symbol]["close"]/bars[first][symbol]["open"]-1-cost_bps/10000)
    # Equal capital is placed in every sector once. This is deliberately a
    # post-hoc diagnostic comparator, never a promotion candidate.
    results.append({"benchmark":"EQUAL_WEIGHT_SECTOR_BUY_HOLD","start":first,"end":last,
                    "total_return":statistics.fmean(returns),"cost_bps":cost_bps,
                    "status":"post_hoc_diagnostic_only"})
    for symbol,value in zip(SECTOR_ETFS,returns):
        results.append({"benchmark":symbol+"_BUY_HOLD","start":first,"end":last,
                        "total_return":value,"cost_bps":cost_bps,"status":"post_hoc_diagnostic_only"})
    return results


def _write_csv(path: Path, rows: list[dict]):
    with path.open("w",newline="",encoding="utf-8") as handle:
        if not rows: return
        writer=csv.DictWriter(handle,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)


def analyze(archive: Path, bars_path: Path, output: Path, strategy="SECTOR_ETF_ROTATION",
            cost_bps=10.0, benchmark_strategy="SPY_BUY_HOLD") -> Path:
    with zipfile.ZipFile(archive) as bundle:
        manifest=json.loads(bundle.read("tier1_manifest.json")); daily=_zip_rows(bundle,"tier1_daily.csv"); trades=_zip_rows(bundle,"tier1_trades.csv")
    config=manifest["config"]; holdout=config.get("holdout_start_date")
    if not holdout: raise ValueError("selected archive has no frozen holdout")
    symbols=set(config["universe_name"] and manifest["universe"]["symbols"]); dates,bars=load_daily_bars(bars_path,symbols)
    candidate=_series(daily,strategy,cost_bps); benchmark=_series(daily,benchmark_strategy,cost_bps)
    if not candidate or not benchmark:
        raise ValueError(f"archive does not contain candidate {strategy} and benchmark {benchmark_strategy}")
    rolling=rolling_comparison(candidate,benchmark,holdout); bootstrap=block_bootstrap(candidate,benchmark,holdout)
    annual=annual_comparison(candidate,benchmark,holdout)
    regimes,regime_definition=regime_attribution(candidate,benchmark,bars,dates,config["discovery_end_date"],holdout)
    attribution=holding_attribution(trades,bars,dates,strategy,cost_bps,holdout); comparators=simple_benchmarks(bars,dates,holdout,cost_bps)
    summary={"selected_strategy":strategy,"benchmark_strategy":benchmark_strategy,"source_archive":archive.name,"cost_bps":cost_bps,"holdout_start":holdout,
             "selection_status":"selected from preregistered Generation 2; no parameters changed in this analysis",
             "bootstrap":bootstrap,"rolling_win_rates":{str(w):sum(r["candidate_won"] for r in rolling if r["window_sessions"]==w)/max(1,sum(1 for r in rolling if r["window_sessions"]==w)) for w in (63,126,252)},
             "calendar_year_win_rate":sum(row["candidate_won"] for row in annual)/max(1,len(annual)),
             "candidate_drawdown":drawdown_profile([row for row in candidate if row["date"] >= holdout]),
             "benchmark_drawdown":drawdown_profile([row for row in benchmark if row["date"] >= holdout]),
             "regime_definition":regime_definition,"post_hoc_benchmarks_are_not_candidates":True}
    output.mkdir(parents=True,exist_ok=True); _write_csv(output/"rolling_windows.csv",rolling); _write_csv(output/"calendar_years.csv",annual); _write_csv(output/"regime_attribution.csv",regimes); _write_csv(output/"holding_attribution.csv",attribution); _write_csv(output/"simple_benchmarks.csv",comparators)
    (output/"robustness_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    return output


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--archive",type=Path,required=True); parser.add_argument("--bars",type=Path,required=True); parser.add_argument("--output",type=Path,required=True); parser.add_argument("--strategy",default="SECTOR_ETF_ROTATION"); parser.add_argument("--benchmark-strategy",default="SPY_BUY_HOLD"); parser.add_argument("--cost-bps",type=float,default=10.0); args=parser.parse_args()
    print(analyze(args.archive,args.bars,args.output,args.strategy,args.cost_bps,args.benchmark_strategy))


if __name__=="__main__": main()
