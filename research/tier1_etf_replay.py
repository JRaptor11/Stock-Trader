"""Daily, long-only ETF strategy tournament for historical research only."""

from __future__ import annotations

import csv
import json
import math
import statistics
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from research.strategy_registry import registry_snapshot, validate_experiment_declaration
from research.universes import resolve_universe, universe_metadata


UTC = timezone.utc
SECTOR_ETFS = ("XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLK", "XLU")
LEGACY_STRATEGIES = (
    "SPY_BUY_HOLD", "VOL_MANAGED_SPY", "ETF_DUAL_MOMENTUM",
    "SECTOR_ETF_ROTATION",
)
STRATEGIES = LEGACY_STRATEGIES + (
    "CROSS_ASSET_DUAL_MOMENTUM",
    "DIVERSIFIED_TREND", "REGIME_BALANCED",
)
CROSS_ASSET_RISK = ("SPY", "QQQ", "IWM", "TLT", "IEF", "GLD", "DBC", "EFA", "EEM", "VNQ")


@dataclass(frozen=True)
class Tier1Config:
    initial_cash: float = 100000.0
    universe_name: str = "ETF_TIER1_RESEARCH"
    benchmark_symbol: str = "SPY"
    cash_proxy_symbol: str = "SHY"
    rebalance_frequency: str = "monthly"
    volatility_lookback_days: int = 60
    volatility_target_annualized: float = 0.12
    trend_lookback_days: int = 200
    momentum_lookbacks_days: tuple[int, ...] = (21, 63, 126, 252)
    sector_holdings: int = 3
    cross_asset_holdings: int = 3
    no_trade_band: float = 0.02
    cost_ladder_bps: tuple[float, ...] = (1.0, 5.0, 10.0, 20.0)
    primary_cost_bps: float = 10.0
    discovery_end_date: str | None = None
    holdout_start_date: str | None = None
    strategy_names: tuple[str, ...] = LEGACY_STRATEGIES

    def __post_init__(self):
        if self.rebalance_frequency not in {"weekly", "monthly"}:
            raise ValueError("rebalance_frequency must be weekly or monthly")
        if self.initial_cash <= 0 or self.sector_holdings < 1 or self.cross_asset_holdings < 1:
            raise ValueError("initial_cash and holding counts must be positive")
        if not self.momentum_lookbacks_days or min(self.momentum_lookbacks_days) < 2:
            raise ValueError("momentum lookbacks must contain positive multi-day windows")
        resolve_universe(self.universe_name)
        if bool(self.discovery_end_date) != bool(self.holdout_start_date):
            raise ValueError("discovery_end_date and holdout_start_date must be set together")
        if self.discovery_end_date and self.discovery_end_date >= self.holdout_start_date:
            raise ValueError("discovery_end_date must precede holdout_start_date")
        if not self.strategy_names:
            raise ValueError("strategy_names must not be empty")
        unknown_strategies = set(self.strategy_names) - set(STRATEGIES)
        if unknown_strategies:
            raise ValueError(f"unknown Tier 1 strategies: {sorted(unknown_strategies)}")
        if len(set(self.strategy_names)) != len(self.strategy_names):
            raise ValueError("strategy_names must not contain duplicates")


def config_from_job(job: dict) -> Tier1Config:
    supplied = dict(job.get("tier1_config") or {})
    allowed = set(Tier1Config.__dataclass_fields__)
    unknown = set(supplied) - allowed
    if unknown:
        raise ValueError(f"unknown tier1_config fields: {sorted(unknown)}")
    for key in ("momentum_lookbacks_days", "cost_ladder_bps", "strategy_names"):
        if key in supplied:
            supplied[key] = tuple(supplied[key])
    return Tier1Config(**supplied)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("bar timestamps must include a timezone")
    return parsed.astimezone(UTC)


def load_daily_bars(path: str | Path, symbols: set[str]) -> tuple[list[str], dict[str, dict[str, dict[str, float]]]]:
    """Load daily bars or aggregate intraday bars into one OHLC row per UTC date."""
    aggregated: dict[tuple[str, str], dict[str, float]] = {}
    with Path(path).open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "symbol", "open", "high", "low", "close"}
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError(f"bar CSV missing columns: {sorted(required - set(reader.fieldnames or ())) }")
        for row in reader:
            symbol = str(row["symbol"]).upper()
            if symbol not in symbols:
                continue
            ts = _timestamp(row["timestamp"]); day = ts.date().isoformat(); key = (day, symbol)
            values = {name: float(row[name]) for name in ("open", "high", "low", "close")}
            current = aggregated.get(key)
            if current is None:
                aggregated[key] = {**values, "first_ts": ts.timestamp(), "last_ts": ts.timestamp()}
            else:
                current["high"] = max(current["high"], values["high"])
                current["low"] = min(current["low"], values["low"])
                if ts.timestamp() < current["first_ts"]:
                    current["open"] = values["open"]; current["first_ts"] = ts.timestamp()
                if ts.timestamp() > current["last_ts"]:
                    current["close"] = values["close"]; current["last_ts"] = ts.timestamp()
    data: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for (day, symbol), values in aggregated.items():
        data[day][symbol] = {key: values[key] for key in ("open", "high", "low", "close")}
    return sorted(data), dict(data)


def _return(closes: list[float], days: int) -> float | None:
    return closes[-1] / closes[-days - 1] - 1.0 if len(closes) > days and closes[-days - 1] else None


def _trend_positive(closes: list[float], days: int) -> bool:
    return len(closes) > days and closes[-1] > statistics.fmean(closes[-days:])


def _rebalance_day(day: str, previous_day: str | None, frequency: str) -> bool:
    date = datetime.fromisoformat(day).date()
    if previous_day is None: return True
    prior = datetime.fromisoformat(previous_day).date()
    return ((date.isocalendar().year, date.isocalendar().week) != (prior.isocalendar().year, prior.isocalendar().week)) if frequency == "weekly" else (date.year, date.month) != (prior.year, prior.month)


def _targets(name: str, histories: dict[str, list[float]], config: Tier1Config) -> dict[str, float]:
    spy = histories.get(config.benchmark_symbol, [])
    if name == "SPY_BUY_HOLD": return {config.benchmark_symbol: 1.0}
    if name == "VOL_MANAGED_SPY":
        returns = [spy[i] / spy[i-1] - 1.0 for i in range(max(1, len(spy)-config.volatility_lookback_days), len(spy)) if spy[i-1]]
        if len(returns) < config.volatility_lookback_days // 2: return {config.cash_proxy_symbol: 1.0}
        vol = statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
        exposure = min(1.0, config.volatility_target_annualized / vol) if vol > 0 else 0.0
        return {config.benchmark_symbol: exposure, config.cash_proxy_symbol: 1.0 - exposure}
    lookbacks = config.momentum_lookbacks_days
    scores = {}
    candidates = [s for s in histories if s != config.cash_proxy_symbol]
    for symbol in candidates:
        values = [_return(histories[symbol], window) for window in lookbacks]
        if all(value is not None for value in values): scores[symbol] = statistics.fmean(values)
    if name == "ETF_DUAL_MOMENTUM":
        eligible = [(score, symbol) for symbol, score in scores.items() if score > 0 and _trend_positive(histories[symbol], config.trend_lookback_days)]
        if not eligible: return {config.cash_proxy_symbol: 1.0}
        symbol = max(eligible)[1]; return {symbol: 1.0}
    if name in {"CROSS_ASSET_DUAL_MOMENTUM", "DIVERSIFIED_TREND"}:
        eligible = [
            (scores[s], s) for s in CROSS_ASSET_RISK
            if s in scores and scores[s] > 0
            and _trend_positive(histories[s], config.trend_lookback_days)
        ]
        selected = [s for _, s in sorted(eligible, reverse=True)[:config.cross_asset_holdings]]
        if not selected:
            return {config.cash_proxy_symbol: 1.0}
        if name == "CROSS_ASSET_DUAL_MOMENTUM":
            return {s: 1.0 / len(selected) for s in selected}
        # Trend breadth deliberately ignores score magnitude after eligibility,
        # reducing concentration in a single recent winner.
        trend_eligible = [
            s for s in CROSS_ASSET_RISK if s in histories
            and _trend_positive(histories[s], config.trend_lookback_days)
        ]
        return {s: 1.0 / len(trend_eligible) for s in trend_eligible} if trend_eligible else {config.cash_proxy_symbol: 1.0}
    if name == "REGIME_BALANCED":
        risk_on = _trend_positive(spy, config.trend_lookback_days)
        if not risk_on:
            defensive = [s for s in ("IEF", "TLT", "GLD") if s in scores and scores[s] > 0]
            return ({s: 1.0 / len(defensive) for s in defensive}
                    if defensive else {config.cash_proxy_symbol: 1.0})
        eligible = [
            (scores[s], s) for s in ("SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ")
            if s in scores and scores[s] > 0
            and _trend_positive(histories[s], config.trend_lookback_days)
        ]
        selected = [s for _, s in sorted(eligible, reverse=True)[:config.cross_asset_holdings]]
        return ({s: 0.8 / len(selected) for s in selected} | {config.cash_proxy_symbol: 0.2}
                if selected else {config.cash_proxy_symbol: 1.0})
    eligible = [(scores[s], s) for s in SECTOR_ETFS if s in scores and scores[s] > 0 and _trend_positive(histories[s], config.trend_lookback_days)]
    selected = [symbol for _, symbol in sorted(eligible, reverse=True)[:config.sector_holdings]]
    if not selected: return {config.cash_proxy_symbol: 1.0}
    return {symbol: 1.0 / len(selected) for symbol in selected}


def _max_drawdown(values: list[float]) -> float:
    peak = values[0]; worst = 0.0
    for value in values:
        peak = max(peak, value); worst = min(worst, value / peak - 1.0)
    return worst


def _metrics(daily: list[dict], initial: float) -> dict:
    equities = [initial] + [row["equity"] for row in daily]
    returns = [equities[i] / equities[i-1] - 1 for i in range(1, len(equities)) if equities[i-1]]
    years = len(returns) / 252.0
    total = equities[-1] / initial - 1.0
    cagr = (equities[-1] / initial) ** (1 / years) - 1 if years > 0 and equities[-1] > 0 else None
    volatility = statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    sharpe = statistics.fmean(returns) / statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 and statistics.stdev(returns) else 0.0
    dd = _max_drawdown(equities)
    return {"total_return": total, "cagr": cagr, "annualized_volatility": volatility, "sharpe": sharpe, "max_drawdown": dd, "calmar": cagr / abs(dd) if cagr is not None and dd else None}


def _period_metrics(daily: list[dict], initial: float, start: str | None, end: str | None) -> dict:
    selected = [row for row in daily if (not start or row["date"] >= start) and (not end or row["date"] <= end)]
    if not selected:
        return {"total_return": None, "cagr": None, "annualized_volatility": None,
                "sharpe": None, "max_drawdown": None, "calmar": None,
                "session_count": 0}
    first_index = daily.index(selected[0])
    starting_equity = daily[first_index - 1]["equity"] if first_index else initial
    return {**_metrics(selected, starting_equity), "session_count": len(selected)}


def _simulate(name: str, dates: list[str], bars: dict, config: Tier1Config, cost_bps: float) -> tuple[list[dict], list[dict]]:
    symbols = resolve_universe(config.universe_name); histories = {s: [] for s in symbols}
    cash = config.initial_cash; shares: dict[str, float] = {}; pending = None; daily=[]; trades=[]; previous_day=None
    for day in dates:
        today=bars[day]
        if pending:
            equity_open = cash + sum(shares.get(s,0)*today.get(s,{"open":0})["open"] for s in shares)
            desired = {s: equity_open*w for s,w in pending.items() if s in today}
            current = {s: shares.get(s,0)*today.get(s,{"open":0})["open"] for s in set(shares)|set(desired)}
            for symbol in sorted(current, key=lambda s: desired.get(s,0)-current.get(s,0)):
                price=today.get(symbol,{}).get("open"); delta=desired.get(symbol,0)-current.get(symbol,0)
                if not price or abs(delta)/max(equity_open,1) < config.no_trade_band: continue
                fee=abs(delta)*cost_bps/10000; cash-=delta+fee; shares[symbol]=shares.get(symbol,0)+delta/price
                if abs(shares[symbol])<1e-10: shares.pop(symbol,None)
                trades.append({"date":day,"strategy":name,"symbol":symbol,"notional":delta,"cost":fee,"cost_bps":cost_bps})
            pending=None
        for symbol in symbols:
            if symbol in today: histories[symbol].append(today[symbol]["close"])
        equity=cash+sum(quantity*today.get(symbol,{"close":0})["close"] for symbol,quantity in shares.items())
        daily.append({"date":day,"strategy":name,"cost_bps":cost_bps,"equity":equity,"cash":cash,"positions":len(shares)})
        if _rebalance_day(day,previous_day,config.rebalance_frequency): pending=_targets(name,histories,config)
        previous_day=day
    return daily,trades


def _write_csv(bundle, name: str, rows: list[dict]):
    if not rows: bundle.writestr(name, "") ; return
    import io
    stream=io.StringIO(); writer=csv.DictWriter(stream,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows); bundle.writestr(name,stream.getvalue())


def run_tier1_job(job: dict, bars_path: Path, archive_path: Path, source_sha256: str, progress_callback=None) -> Path:
    config=config_from_job(job); symbols=set(resolve_universe(config.universe_name))
    dates,bars=load_daily_bars(bars_path,symbols)
    if len(dates) <= max(config.momentum_lookbacks_days): raise ValueError("insufficient daily history for Tier 1 lookbacks")
    all_daily=[]; all_trades=[]; scorecards=[]; period_scorecards=[]
    tasks=[(s,c) for c in config.cost_ladder_bps for s in config.strategy_names]
    for index,(strategy,cost) in enumerate(tasks,1):
        daily,trades=_simulate(strategy,dates,bars,config,float(cost)); metrics=_metrics(daily,config.initial_cash)
        all_daily.extend(daily); all_trades.extend(trades)
        scorecards.append({"strategy":strategy,"cost_bps":float(cost),**metrics,"turnover":sum(abs(r["notional"]) for r in trades)/config.initial_cash,"trade_count":len(trades)})
        periods = [("full", None, None)]
        if config.discovery_end_date:
            periods.extend([
                ("discovery", None, config.discovery_end_date),
                ("holdout", config.holdout_start_date, None),
            ])
        for period, start, end in periods:
            period_scorecards.append({
                "strategy": strategy, "cost_bps": float(cost), "period": period,
                "period_start": start, "period_end": end,
                **_period_metrics(daily, config.initial_cash, start, end),
            })
        if progress_callback: progress_callback({"stage":"tier1_strategy_tournament","stage_completed_rows":index,"stage_total_rows":len(tasks),"stage_percent_complete":round(index/len(tasks)*100,2)})
    promotion_period = "holdout" if config.holdout_start_date else "full"
    primary={r["strategy"]:r for r in period_scorecards if r["cost_bps"]==config.primary_cost_bps and r["period"]==promotion_period}; benchmark=primary["SPY_BUY_HOLD"]
    promotions=[]
    for strategy,row in sorted(primary.items()):
        if strategy=="SPY_BUY_HOLD": continue
        beats=row["total_return"]>benchmark["total_return"]
        risk_improves=row["max_drawdown"]>benchmark["max_drawdown"] or row["sharpe"]>benchmark["sharpe"]
        promotions.append({"strategy":strategy,"benchmark":"SPY_BUY_HOLD","evaluation_period":promotion_period,"net_excess_return":row["total_return"]-benchmark["total_return"],"return_gate":beats,"risk_gate":risk_improves,"eligible_for_further_validation":beats and risk_improves,"paper_trading_approved":False})
    declaration=validate_experiment_declaration(job.get("experiment"))
    manifest={"created_at":datetime.now(UTC).isoformat(),"engine":"tier1_etf_daily","source_path":str(bars_path),"source_sha256":source_sha256,"config":asdict(config),"universe":universe_metadata(config.universe_name,tuple(sorted(symbols))),"hypothesis_registry":registry_snapshot(),"experiment":declaration,"execution_semantics":"signal_at_close_fill_at_next_available_open","strategies":list(config.strategy_names),"promotion_policy":"diagnostic gate only; shadow approval requires untouched holdout and stability tests"}
    archive_path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive_path,"w",zipfile.ZIP_DEFLATED,compresslevel=1) as bundle:
        _write_csv(bundle,"tier1_daily.csv",all_daily); _write_csv(bundle,"tier1_trades.csv",all_trades); _write_csv(bundle,"tier1_cost_ladder_scorecard.csv",scorecards); _write_csv(bundle,"tier1_period_scorecard.csv",period_scorecards); _write_csv(bundle,"tier1_promotion_gates.csv",promotions)
        bundle.writestr("tier1_manifest.json",json.dumps(manifest,indent=2,default=list)); bundle.writestr("tier1_summary.json",json.dumps({"primary_cost_bps":config.primary_cost_bps,"promotion_period":promotion_period,"scorecards":list(primary.values()),"promotion_gates":promotions},indent=2))
    return archive_path
