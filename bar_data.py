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