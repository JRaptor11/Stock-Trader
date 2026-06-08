import logging
from datetime import datetime, timedelta, timezone

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest


def fetch_recent_bars(data_client, symbols, lookback_hours=24, timeframe_minutes=15):
    if not data_client:
        logging.warning("[Bars] No Alpaca data client available.")
        return {}

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=lookback_hours)

    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
        start=start,
        end=end,
    )

    try:
        bars = data_client.get_stock_bars(request)
    except Exception:
        logging.exception("[Bars] Failed to fetch stock bars.")
        return {}

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

    return result