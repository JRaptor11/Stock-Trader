"""Retrospective breakout-opportunity labels compared with causal strategy signals."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict


BREAKOUT_STRATEGIES = (
    "SECTOR_PRICE_BREAKOUT_20D", "DONCHIAN_TREND_BREAKOUT",
    "VOLATILITY_CONTRACTION_BREAKOUT", "RELATIVE_STRENGTH_BREAKOUT",
)
THRESHOLDS = (0.03, 0.05, 0.10)


def _net_return(value: float, cost_bps: float) -> float:
    return (1.0 + value) * (1.0 - cost_bps / 10000.0) / (1.0 + cost_bps / 10000.0) - 1.0


def build_breakout_opportunity_diagnostics(
    dates, bars, config, scored_start, state_labels, target_function, sector_symbols,
):
    """Measure signal precision and opportunity recall without using labels in targets."""
    strategies = [name for name in config.strategy_names if name in BREAKOUT_STRATEGIES]
    if not strategies:
        return [], []
    symbols = tuple(bars[dates[0]])
    date_index = {day: index for index, day in enumerate(dates)}
    states = {row["date"]: row["core_state"] for row in state_labels}
    signals = defaultdict(set)
    for strategy in strategies:
        histories = {symbol: [] for symbol in symbols}
        for index, day in enumerate(dates):
            for symbol in symbols:
                if symbol in bars[day]:
                    histories[symbol].append(float(bars[day][symbol]["close"]))
            if day < scored_start or index + 1 >= len(dates):
                continue
            targets = target_function(strategy, histories, config)
            for symbol, weight in targets.items():
                if symbol in sector_symbols and weight > 0:
                    signals[(strategy, dates[index + 1])].add(symbol)

    rows = []
    for symbol in sector_symbols:
        qualifying_previous = {threshold: False for threshold in THRESHOLDS}
        for entry_index, entry_day in enumerate(dates):
            if entry_day < scored_start or entry_index + 5 >= len(dates):
                continue
            entry = float(bars[entry_day][symbol]["open"])
            window = [bars[dates[i]][symbol] for i in range(entry_index, entry_index + 5)]
            mfe = max(float(bar["high"]) for bar in window) / entry - 1.0
            for threshold in THRESHOLDS:
                qualifies = mfe >= threshold
                onset = qualifies and not qualifying_previous[threshold]
                qualifying_previous[threshold] = qualifies
                if not onset:
                    continue
                for strategy in strategies:
                    detected_day = None
                    for delay in range(3):
                        candidate_index = entry_index + delay
                        if candidate_index < len(dates) and symbol in signals.get(
                            (strategy, dates[candidate_index]), set()
                        ):
                            detected_day = dates[candidate_index]
                            break
                    detected = detected_day is not None
                    net_5d = None
                    capture = None
                    if detected:
                        signal_index = date_index[detected_day]
                        if signal_index + 5 < len(dates):
                            raw = (float(bars[dates[signal_index + 5]][symbol]["open"])
                                   / float(bars[detected_day][symbol]["open"]) - 1.0)
                            net_5d = _net_return(raw, config.primary_cost_bps)
                            capture = net_5d / mfe if mfe > 0 else None
                    rows.append({
                        "strategy": strategy, "symbol": symbol, "opportunity_date": entry_day,
                        "core_state": states.get(entry_day), "threshold": threshold,
                        "opportunity_mfe_5d": mfe, "detected": detected,
                        "detected_entry_date": detected_day,
                        "detection_delay_sessions": (date_index[detected_day] - entry_index
                                                     if detected else None),
                        "detected_net_5d_return": net_5d,
                        "capture_of_opportunity_mfe": capture,
                    })
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["strategy"], row["threshold"], "ALL")].append(row)
        grouped[(row["strategy"], row["threshold"], row["core_state"] or "UNKNOWN")].append(row)
    summary = []
    for (strategy, threshold, state), group in sorted(grouped.items()):
        detected = [row for row in group if row["detected"]]
        returns = [row["detected_net_5d_return"] for row in detected
                   if row["detected_net_5d_return"] is not None]
        captures = [row["capture_of_opportunity_mfe"] for row in detected
                    if row["capture_of_opportunity_mfe"] is not None]
        summary.append({
            "strategy": strategy, "threshold": threshold, "core_state": state,
            "opportunities": len(group), "detected_opportunities": len(detected),
            "recall_within_2_sessions": len(detected) / len(group),
            "median_detection_delay_sessions": statistics.median(
                row["detection_delay_sessions"] for row in detected) if detected else None,
            "profitable_5d_capture_rate": sum(value > 0 for value in returns) / len(returns)
                                          if returns else None,
            "mean_detected_5d_return": statistics.fmean(returns) if returns else None,
            "capture_at_least_30pct_rate": sum(value >= .30 for value in captures) / len(captures)
                                           if captures else None,
        })
    return rows, summary
