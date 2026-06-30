# market/bar_data.py

import logging
from datetime import datetime, timedelta, timezone

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed

def fetch_recent_bars(data_client, symbols, lookback_hours=24, timeframe_minutes=15):
    if not data_client:
        logging.warning("[Bars] No Alpaca data client available.")
        return {}

    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(hours=lookback_hours)

    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )

    try:
        bars = data_client.get_stock_bars(request)

        logging.info(
            "[Bars] Fetching %s-minute IEX bars | symbols=%s start=%s end=%s",
            timeframe_minutes,
            symbols,
            start,
            end,
        )

    except Exception as e:
        logging.exception(
            "[Bars] Failed to fetch stock bars | symbols=%s start=%s end=%s error=%s",
            symbols,
            start,
            end,
            e,
        )
        return {}

    try:
        raw_symbols = list(getattr(bars, "data", {}).keys())
        logging.info("[Bars] Raw response symbols: %s", raw_symbols)
    except Exception:
        logging.warning("[Bars] Could not inspect raw bar response.")

    result = {}

    for symbol in symbols:
        try:
            symbol_bars = bars.data.get(symbol, [])
            result[symbol] = [
                {
                    "timestamp": bar.timestamp,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                    "trade_count": float(getattr(bar, "trade_count", 0) or 0),
                    "vwap": float(getattr(bar, "vwap", 0) or 0),
                }
                for bar in symbol_bars
            ]
        except Exception:
            logging.exception("[Bars] Failed parsing bars for %s", symbol)
            result[symbol] = []

    latest_bar_times = {}
    latest_bar_ages_minutes = {}

    now_utc = datetime.now(timezone.utc)

    for symbol, symbol_bars in result.items():
        if not symbol_bars:
            latest_bar_times[symbol] = None
            latest_bar_ages_minutes[symbol] = None
            continue

        latest_ts = symbol_bars[-1].get("timestamp")

        latest_bar_times[symbol] = latest_ts

        if latest_ts:
            latest_bar_ages_minutes[symbol] = round(
                (now_utc - latest_ts).total_seconds() / 60,
                1,
            )
        else:
            latest_bar_ages_minutes[symbol] = None

    logging.info("[Bars] Latest bar times: %s", latest_bar_times)
    logging.info("[Bars] Latest bar ages minutes: %s", latest_bar_ages_minutes)

    return result


def _to_aware_utc(dt):
    if dt is None:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def latest_bar_age_minutes(symbol_bars: list, now_utc: datetime | None = None) -> float | None:
    """
    Return the age in minutes of the latest bar in a symbol's bar list.
    """
    if not symbol_bars:
        return None

    latest_ts = symbol_bars[-1].get("timestamp")
    latest_ts = _to_aware_utc(latest_ts)

    if latest_ts is None:
        return None

    now_utc = now_utc or datetime.now(timezone.utc)
    return round((now_utc - latest_ts).total_seconds() / 60, 1)


def build_bar_freshness_report(
    bars_by_symbol: dict,
    symbols: list,
    *,
    max_age_minutes: float,
    now_utc: datetime | None = None,
) -> dict:
    """
    Build freshness diagnostics for the latest bars.

    A symbol is fresh only if:
    - it has bars
    - latest bar timestamp exists
    - latest bar age is <= max_age_minutes
    """
    now_utc = now_utc or datetime.now(timezone.utc)

    latest_bar_times = {}
    latest_bar_ages_minutes = {}
    fresh_symbols = []
    stale_symbols = []
    missing_symbols = []

    for symbol in symbols:
        symbol_bars = bars_by_symbol.get(symbol, []) or []

        if not symbol_bars:
            latest_bar_times[symbol] = None
            latest_bar_ages_minutes[symbol] = None
            missing_symbols.append(symbol)
            stale_symbols.append(symbol)
            continue

        latest_ts = _to_aware_utc(symbol_bars[-1].get("timestamp"))
        latest_bar_times[symbol] = latest_ts

        age_minutes = latest_bar_age_minutes(symbol_bars, now_utc=now_utc)
        latest_bar_ages_minutes[symbol] = age_minutes

        if age_minutes is not None and age_minutes <= max_age_minutes:
            fresh_symbols.append(symbol)
        else:
            stale_symbols.append(symbol)

    return {
        "max_age_minutes": float(max_age_minutes),
        "fresh_symbols": fresh_symbols,
        "stale_symbols": stale_symbols,
        "missing_symbols": missing_symbols,
        "fresh_count": len(fresh_symbols),
        "stale_count": len(stale_symbols),
        "total_symbols": len(symbols),
        "latest_bar_times": latest_bar_times,
        "latest_bar_ages_minutes": latest_bar_ages_minutes,
    }


def filter_fresh_bars(
    bars_by_symbol: dict,
    symbols: list,
    *,
    max_age_minutes: float,
) -> tuple[dict, dict]:
    """
    Return:
    - bars only for fresh symbols
    - freshness report
    """
    report = build_bar_freshness_report(
        bars_by_symbol,
        symbols,
        max_age_minutes=max_age_minutes,
    )

    fresh_set = set(report["fresh_symbols"])

    fresh_bars = {
        symbol: bars
        for symbol, bars in bars_by_symbol.items()
        if symbol in fresh_set
    }

    return fresh_bars, report


def fetch_recent_bars_with_min_count(
    data_client,
    symbols,
    *,
    min_bars: int = 60,
    timeframe_minutes: int = 15,
    initial_lookback_hours: int = 48,
    max_lookback_hours: int = 240,
    min_ready_symbols: int | None = None,
):
    """
    Fetch recent bars, expanding the calendar lookback until most symbols
    have enough actual market bars.

    This avoids Monday/holiday problems where 48 calendar hours may contain
    very few trading bars.
    """
    if not symbols:
        return {}

    if min_ready_symbols is None:
        min_ready_symbols = 1

    min_ready_symbols = max(1, min(int(min_ready_symbols), len(symbols)))

    lookback_hours = initial_lookback_hours
    latest_result = {}

    while lookback_hours <= max_lookback_hours:
        latest_result = fetch_recent_bars(
            data_client,
            symbols,
            lookback_hours=lookback_hours,
            timeframe_minutes=timeframe_minutes,
        )

        bar_counts = {
            symbol: len(latest_result.get(symbol, []))
            for symbol in symbols
        }

        symbols_with_enough_bars = [
            symbol
            for symbol, count in bar_counts.items()
            if count >= min_bars
        ]

        logging.info(
            "[Bars] Min-count check | lookback_hours=%s min_bars=%s "
            "symbols_ready=%s/%s bar_counts=%s",
            lookback_hours,
            min_bars,
            len(symbols_with_enough_bars),
            len(symbols),
            bar_counts,
        )

        # Good enough if at least one symbol can be ranked.
        # Better if most symbols are ready.
        if len(symbols_with_enough_bars) >= min_ready_symbols:
            return latest_result

        lookback_hours *= 2

    logging.warning(
        "[Bars] Could not reach min_bars=%s within max_lookback_hours=%s. "
        "Returning latest result anyway.",
        min_bars,
        max_lookback_hours,
    )

    return latest_result