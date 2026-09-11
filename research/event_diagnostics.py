"""Event-level diagnostics for isolated daily research strategies."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict


EVENT_STRATEGIES = ("SECTOR_PRICE_BREAKOUT_20D", "MARKET_DIP_REBOUND_1D")
HORIZONS = (1, 2, 3, 5, 10)
TRAILING_STOPS = (0.03, 0.05)


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def _event_summary(rows: list[dict], cost_ladder_bps: tuple[float, ...]) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["strategy"], row["cost_bps"])].append(row)
    result = []
    for (strategy, cost), events in sorted(grouped.items()):
        returns = [float(row["net_return"]) for row in events]
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        gross_profit = sum(wins)
        ordered_wins = sorted(wins, reverse=True)
        result.append({
            "strategy": strategy,
            "cost_bps": cost,
            "events": len(events),
            "completed_events": sum(not row["censored"] for row in events),
            "censored_events": sum(bool(row["censored"]) for row in events),
            "hit_rate": len(wins) / len(events),
            "mean_net_return": statistics.fmean(returns),
            "median_net_return": statistics.median(returns),
            "p05_net_return": _quantile(returns, 0.05),
            "p95_net_return": _quantile(returns, 0.95),
            "average_win": statistics.fmean(wins) if wins else None,
            "average_loss": statistics.fmean(losses) if losses else None,
            "payoff_ratio": (statistics.fmean(wins) / abs(statistics.fmean(losses))
                              if wins and losses else None),
            "top_5pct_gross_profit_share": (
                sum(ordered_wins[:max(1, math.ceil(len(events) * 0.05))]) / gross_profit
                if gross_profit else None
            ),
            "mean_holding_sessions": statistics.fmean(
                float(row["holding_sessions"]) for row in events
            ),
            "mean_entry_gap": statistics.fmean(float(row["entry_gap"]) for row in events),
            "mean_mfe_5d": statistics.fmean(float(row["mfe_5d"]) for row in events),
            "mean_mae_5d": statistics.fmean(float(row["mae_5d"]) for row in events),
            "duplicate_signal_count": sum(int(row["duplicate_signal_count"]) for row in events),
        })
    return result


def _net_return(gross_return: float, cost_bps: float) -> float:
    entry_multiplier = 1.0 + cost_bps / 10000.0
    exit_multiplier = 1.0 - cost_bps / 10000.0
    return (1.0 + gross_return) * exit_multiplier / entry_multiplier - 1.0


def _horizon_summary(rows: list[dict]) -> list[dict]:
    result = []
    for strategy in sorted({row["strategy"] for row in rows}):
        strategy_rows = [row for row in rows if row["strategy"] == strategy]
        for cost in sorted({float(row["cost_bps"]) for row in strategy_rows}):
            events = [row for row in strategy_rows if float(row["cost_bps"]) == cost]
            for horizon in HORIZONS:
                gross = [float(row[f"exit_{horizon}d_return"]) for row in events
                         if row[f"exit_{horizon}d_return"] is not None]
                net = [_net_return(value, cost) for value in gross]
                result.append({
                    "strategy": strategy, "cost_bps": cost, "horizon_sessions": horizon,
                    "events": len(net),
                    "mean_net_return": statistics.fmean(net) if net else None,
                    "median_net_return": statistics.median(net) if net else None,
                    "hit_rate": sum(value > 0 for value in net) / len(net) if net else None,
                    "p05_net_return": _quantile(net, 0.05),
                    "p95_net_return": _quantile(net, 0.95),
                })
    return result


def _condition_summary(rows: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        for dimension, bucket in row.items():
            if dimension.endswith("_bucket") and bucket:
                grouped[(row["strategy"], float(row["cost_bps"]), dimension, bucket)].append(
                    float(row["net_return"])
                )
    return [
        {
            "strategy": key[0], "cost_bps": key[1], "dimension": key[2],
            "bucket": key[3], "events": len(values),
            "mean_net_return": statistics.fmean(values),
            "median_net_return": statistics.median(values),
            "hit_rate": sum(value > 0 for value in values) / len(values),
        }
        for key, values in sorted(grouped.items())
    ]


def build_event_diagnostics(
    dates: list[str], bars: dict, config, scored_start: str, market_conditions: dict,
    target_function,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Reconstruct signal episodes and fixed-horizon outcomes without retuning rules."""
    selected = [name for name in config.strategy_names if name in EVENT_STRATEGIES]
    if not selected:
        return [], [], [], []
    symbols = tuple(bars[dates[0]])
    date_index = {day: index for index, day in enumerate(dates)}
    rows = []
    for strategy in selected:
        histories = {symbol: [] for symbol in symbols}
        desired_by_entry = {}
        for index, day in enumerate(dates):
            for symbol in symbols:
                if symbol in bars[day]:
                    histories[symbol].append(float(bars[day][symbol]["close"]))
            if day < scored_start or index + 1 >= len(dates):
                continue
            targets = target_function(strategy, histories, config)
            risk = [(weight, symbol) for symbol, weight in targets.items()
                    if symbol != config.cash_proxy_symbol and weight > 0]
            desired_by_entry[dates[index + 1]] = {
                "symbol": max(risk)[1] if risk else None,
                "signal_date": day,
            }

        episodes = []
        active = None
        for entry_day in sorted(desired_by_entry, key=date_index.get):
            desired = desired_by_entry[entry_day]
            symbol = desired["symbol"]
            if active and active["symbol"] == symbol:
                active["duplicate_signal_count"] += 1
                continue
            if active:
                active.update({
                    "exit_date": entry_day,
                    "exit_price": float(bars[entry_day][active["symbol"]]["open"]),
                    "censored": False,
                })
                episodes.append(active)
                active = None
            if symbol:
                signal_close = float(bars[desired["signal_date"]][symbol]["close"])
                entry_price = float(bars[entry_day][symbol]["open"])
                active = {
                    "strategy": strategy, "symbol": symbol,
                    "signal_date": desired["signal_date"], "entry_date": entry_day,
                    "entry_price": entry_price,
                    "entry_gap": entry_price / signal_close - 1.0,
                    "duplicate_signal_count": 0,
                }
        if active:
            last_day = dates[-1]
            active.update({
                "exit_date": last_day,
                "exit_price": float(bars[last_day][active["symbol"]]["close"]),
                "censored": True,
            })
            episodes.append(active)

        for event_number, event in enumerate(episodes, 1):
            entry_index = date_index[event["entry_date"]]
            exit_index = date_index[event["exit_date"]]
            entry_price = event["entry_price"]
            gross_return = event["exit_price"] / entry_price - 1.0
            base = {
                **event, "event_id": f"{strategy}:{event_number:05d}",
                "holding_sessions": max(0, exit_index - entry_index),
                "gross_return": gross_return,
            }
            for horizon in HORIZONS:
                through = min(len(dates) - 1, entry_index + horizon - 1)
                window = [bars[dates[index]][event["symbol"]]
                          for index in range(entry_index, through + 1)]
                base[f"mfe_{horizon}d"] = max(bar["high"] for bar in window) / entry_price - 1.0
                base[f"mae_{horizon}d"] = min(bar["low"] for bar in window) / entry_price - 1.0
                exit_at = entry_index + horizon
                base[f"exit_{horizon}d_return"] = (
                    float(bars[dates[exit_at]][event["symbol"]]["open"]) / entry_price - 1.0
                    if exit_at < len(dates) else None
                )
            # Daily bars cannot reveal the intraday high/low order. This
            # deliberately conservative approximation checks the low against
            # the prior peak before allowing that session's high to raise it.
            for stop in TRAILING_STOPS:
                peak = entry_price
                stopped_return = None
                stopped_date = None
                for future_index in range(entry_index, min(len(dates), entry_index + 10)):
                    future_day = dates[future_index]
                    bar = bars[future_day][event["symbol"]]
                    stop_price = peak * (1.0 - stop)
                    if float(bar["open"]) <= stop_price:
                        stopped_return = float(bar["open"]) / entry_price - 1.0
                        stopped_date = future_day
                        break
                    if float(bar["low"]) <= stop_price:
                        stopped_return = stop_price / entry_price - 1.0
                        stopped_date = future_day
                        break
                    peak = max(peak, float(bar["high"]))
                label = int(stop * 100)
                base[f"trailing_stop_{label}pct_return"] = stopped_return
                base[f"trailing_stop_{label}pct_date"] = stopped_date
            if strategy == "SECTOR_PRICE_BREAKOUT_20D":
                signal_index = date_index[event["signal_date"]]
                failed_date = None
                failed_return = None
                if signal_index >= 20:
                    threshold = max(
                        float(bars[dates[index]][event["symbol"]]["close"])
                        for index in range(signal_index - 20, signal_index)
                    )
                    for future_index in range(
                        entry_index, min(len(dates) - 1, entry_index + 10)
                    ):
                        future_day = dates[future_index]
                        if float(bars[future_day][event["symbol"]]["close"]) < threshold:
                            failed_date = dates[future_index + 1]
                            failed_return = (
                                float(bars[failed_date][event["symbol"]]["open"])
                                / entry_price - 1.0
                            )
                            break
                base["failed_breakout_exit_date"] = failed_date
                base["failed_breakout_exit_return"] = failed_return
            else:
                base["failed_breakout_exit_date"] = None
                base["failed_breakout_exit_return"] = None
            base["capture_of_5d_mfe"] = (
                gross_return / base["mfe_5d"] if base["mfe_5d"] > 0 else None
            )
            state = market_conditions.get(event["entry_date"], {})
            base.update({key: value for key, value in state.items() if key.endswith("_bucket")})
            for cost in config.cost_ladder_bps:
                rows.append({
                    **base, "cost_bps": float(cost),
                    "net_return": _net_return(gross_return, float(cost)),
                })
    return (
        rows, _event_summary(rows, config.cost_ladder_bps),
        _horizon_summary(rows), _condition_summary(rows),
    )
