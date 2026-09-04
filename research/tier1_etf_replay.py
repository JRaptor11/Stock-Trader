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
from research.walk_forward import build_walk_forward_folds


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
    required_common_start_date: str | None = None
    minimum_common_coverage_pct: float = 99.0
    minimum_scored_sessions: int = 1
    rolling_window_sessions: int = 756
    walk_forward_train_sessions: int = 504
    walk_forward_test_sessions: int = 252
    walk_forward_step_sessions: int = 252

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
        if not 0 < self.minimum_common_coverage_pct <= 100:
            raise ValueError("minimum_common_coverage_pct must be above zero and at most 100")
        for name in ("minimum_scored_sessions", "rolling_window_sessions",
                     "walk_forward_train_sessions", "walk_forward_test_sessions",
                     "walk_forward_step_sessions"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")


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


def _validated_calendar(dates: list[str], bars: dict, symbols: tuple[str, ...],
                        config: Tier1Config) -> tuple[list[str], dict]:
    """Build a continuous common calendar and reject misleading coverage."""
    coverage = []
    for symbol in symbols:
        available = [day for day in dates if symbol in bars[day]]
        if not available:
            raise ValueError(f"historical dataset has no bars for {symbol}")
        coverage.append({"symbol": symbol, "first": available[0],
                         "last": available[-1], "rows": len(available)})
    common_start = max(row["first"] for row in coverage)
    common_end = min(row["last"] for row in coverage)
    if common_start > common_end:
        raise ValueError("historical symbols have no overlapping date range")
    if config.required_common_start_date and common_start > config.required_common_start_date:
        raise ValueError(
            "historical common coverage begins later than required: "
            f"{common_start} > {config.required_common_start_date}"
        )
    benchmark_dates = [
        day for day in dates if common_start <= day <= common_end
        and config.benchmark_symbol in bars[day]
    ]
    common_dates = [day for day in benchmark_dates if all(symbol in bars[day] for symbol in symbols)]
    common_pct = len(common_dates) / len(benchmark_dates) * 100 if benchmark_dates else 0.0
    if common_pct < config.minimum_common_coverage_pct:
        raise ValueError(
            "historical common-session coverage is below the configured minimum: "
            f"{common_pct:.4f}% < {config.minimum_common_coverage_pct:.4f}%"
        )
    warmup = max(
        max(config.momentum_lookbacks_days) + 1,
        config.trend_lookback_days,
        config.volatility_lookback_days + 1,
    )
    if len(common_dates) <= warmup:
        raise ValueError("insufficient common daily history after warm-up")
    scored_dates = common_dates[warmup:]
    if len(scored_dates) < config.minimum_scored_sessions:
        raise ValueError(
            f"only {len(scored_dates)} scored sessions remain after the {warmup}-session warm-up; "
            f"minimum is {config.minimum_scored_sessions}"
        )
    report = {
        "raw_start": dates[0], "raw_end": dates[-1],
        "common_start": common_start, "common_end": common_end,
        "common_session_count": len(common_dates),
        "benchmark_session_count": len(benchmark_dates),
        "common_coverage_pct": round(common_pct, 4),
        "warmup_sessions": warmup, "scored_start": scored_dates[0],
        "scored_end": scored_dates[-1], "scored_sessions": len(scored_dates),
        "symbols": coverage,
    }
    return common_dates, report


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


def _simulate(name: str, dates: list[str], bars: dict, config: Tier1Config,
              cost_bps: float, scored_start: str) -> tuple[list[dict], list[dict]]:
    symbols = resolve_universe(config.universe_name); histories = {s: [] for s in symbols}
    cash = config.initial_cash; shares: dict[str, float] = {}; pending = None; daily=[]; trades=[]; previous_day=None
    for index, day in enumerate(dates):
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
        if day >= scored_start:
            daily.append({"date":day,"strategy":name,"cost_bps":cost_bps,"equity":equity,"cash":cash,"positions":len(shares)})
            if _rebalance_day(day,previous_day,config.rebalance_frequency):
                pending=_targets(name,histories,config)
            previous_day=day
        elif index + 1 < len(dates) and dates[index + 1] == scored_start:
            # Compute the first target after the final warm-up close so the
            # first scored session receives the same next-open semantics.
            pending=_targets(name,histories,config)
    return daily,trades


def _rolling_scorecards(daily: list[dict], config: Tier1Config) -> list[dict]:
    selected = [row for row in daily if row["cost_bps"] == config.primary_cost_bps]
    by_strategy = {name: [r for r in selected if r["strategy"] == name]
                   for name in config.strategy_names}
    benchmark = by_strategy["SPY_BUY_HOLD"]
    dates = [row["date"] for row in benchmark]
    rows = []
    window, step = config.rolling_window_sessions, 63
    for end in range(window, len(dates) + 1, step):
        start_day, end_day = dates[end-window], dates[end-1]
        benchmark_metrics = _period_metrics(
            benchmark, config.initial_cash, start_day, end_day
        )
        for strategy, values in by_strategy.items():
            metrics = _period_metrics(values, config.initial_cash, start_day, end_day)
            rows.append({
                "strategy": strategy, "cost_bps": config.primary_cost_bps,
                "window_sessions": window, "start": start_day, "end": end_day,
                **metrics,
                "benchmark_return": benchmark_metrics["total_return"],
                "excess_return": metrics["total_return"] - benchmark_metrics["total_return"],
                "beat_benchmark": metrics["total_return"] > benchmark_metrics["total_return"],
            })
    return rows


def _walk_forward_scorecards(daily: list[dict], config: Tier1Config) -> list[dict]:
    selected = [row for row in daily if row["cost_bps"] == config.primary_cost_bps]
    by_strategy = {name: [r for r in selected if r["strategy"] == name]
                   for name in config.strategy_names}
    benchmark = by_strategy["SPY_BUY_HOLD"]
    dates = [row["date"] for row in benchmark]
    folds = build_walk_forward_folds(
        dates, min_train_sessions=config.walk_forward_train_sessions,
        test_sessions=config.walk_forward_test_sessions,
        step_sessions=config.walk_forward_step_sessions, expanding=True,
    )
    rows = []
    for fold in folds:
        train_metrics = {}
        for strategy, values in by_strategy.items():
            train_metrics[strategy] = _period_metrics(
                values, config.initial_cash, fold.train_start, fold.train_end
            )
        ranked = sorted(
            config.strategy_names,
            key=lambda strategy: (
                train_metrics[strategy]["sharpe"],
                train_metrics[strategy]["total_return"],
            ), reverse=True,
        )
        benchmark_test = _period_metrics(
            benchmark, config.initial_cash, fold.test_start, fold.test_end
        )
        for strategy, values in by_strategy.items():
            test = _period_metrics(values, config.initial_cash, fold.test_start, fold.test_end)
            train = train_metrics[strategy]
            rows.append({
                "fold": fold.fold, "strategy": strategy,
                "train_start": fold.train_start, "train_end": fold.train_end,
                "test_start": fold.test_start, "test_end": fold.test_end,
                "train_rank": ranked.index(strategy) + 1,
                "selected_on_train": strategy == ranked[0],
                "train_return": train["total_return"], "train_sharpe": train["sharpe"],
                "test_return": test["total_return"], "test_sharpe": test["sharpe"],
                "test_max_drawdown": test["max_drawdown"],
                "benchmark_test_return": benchmark_test["total_return"],
                "test_excess_return": test["total_return"] - benchmark_test["total_return"],
                "beat_benchmark_in_test": test["total_return"] > benchmark_test["total_return"],
            })
    return rows


def _write_csv(bundle, name: str, rows: list[dict]):
    if not rows: bundle.writestr(name, "") ; return
    import io
    stream=io.StringIO(); writer=csv.DictWriter(stream,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows); bundle.writestr(name,stream.getvalue())


def run_tier1_job(job: dict, bars_path: Path, archive_path: Path, source_sha256: str, progress_callback=None) -> Path:
    config=config_from_job(job); universe=resolve_universe(config.universe_name); symbols=set(universe)
    raw_dates,bars=load_daily_bars(bars_path,symbols)
    dates,coverage=_validated_calendar(raw_dates,bars,universe,config)
    scored_start=coverage["scored_start"]
    all_daily=[]; all_trades=[]; scorecards=[]; period_scorecards=[]
    tasks=[(s,c) for c in config.cost_ladder_bps for s in config.strategy_names]
    for index,(strategy,cost) in enumerate(tasks,1):
        daily,trades=_simulate(strategy,dates,bars,config,float(cost),scored_start); metrics=_metrics(daily,config.initial_cash)
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
    rolling_scorecards=_rolling_scorecards(all_daily,config)
    walk_forward_scorecards=_walk_forward_scorecards(all_daily,config)
    declaration=validate_experiment_declaration(job.get("experiment"))
    manifest={"created_at":datetime.now(UTC).isoformat(),"engine":"tier1_etf_daily","source_path":str(bars_path),"source_sha256":source_sha256,"config":asdict(config),"coverage":coverage,"universe":universe_metadata(config.universe_name,tuple(sorted(symbols))),"hypothesis_registry":registry_snapshot(),"experiment":declaration,"execution_semantics":"warm-up excluded; signal at close and fill at next available open on validated common sessions","strategies":list(config.strategy_names),"promotion_policy":"diagnostic gate only; shadow approval requires untouched holdout and stability tests"}
    archive_path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive_path,"w",zipfile.ZIP_DEFLATED,compresslevel=1) as bundle:
        _write_csv(bundle,"tier1_daily.csv",all_daily); _write_csv(bundle,"tier1_trades.csv",all_trades); _write_csv(bundle,"tier1_cost_ladder_scorecard.csv",scorecards); _write_csv(bundle,"tier1_period_scorecard.csv",period_scorecards); _write_csv(bundle,"tier1_promotion_gates.csv",promotions); _write_csv(bundle,"tier1_rolling_3y_scorecard.csv",rolling_scorecards); _write_csv(bundle,"tier1_walk_forward_scorecard.csv",walk_forward_scorecards)
        bundle.writestr("tier1_manifest.json",json.dumps(manifest,indent=2,default=list)); bundle.writestr("tier1_summary.json",json.dumps({"primary_cost_bps":config.primary_cost_bps,"promotion_period":promotion_period,"scorecards":list(primary.values()),"promotion_gates":promotions},indent=2))
    return archive_path
