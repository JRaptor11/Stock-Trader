# market/bar_data.py

import logging
from datetime import datetime, timedelta, timezone

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed


def _latest_bar_metadata(result: dict) -> tuple[dict, dict]:
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

    return latest_bar_times, latest_bar_ages_minutes


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
        logging.debug("[Bars] Raw response symbols: %s", raw_symbols)
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

    logging.debug("[Bars] Latest bar times: %s", latest_bar_times)
    logging.info("[Bars] Latest bar ages minutes: %s", latest_bar_ages_minutes)

    return result


def _to_aware_utc(dt):
    if dt is None:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def _bar_field(
    bar,
    key: str,
    default=None,
):
    if isinstance(
        bar,
        dict,
    ):
        return bar.get(
            key,
            default,
        )

    return getattr(
        bar,
        key,
        default,
    )


def _timestamp_utc(
    value,
) -> datetime | None:
    if value is None:
        return None

    if isinstance(
        value,
        datetime,
    ):
        if value.tzinfo is None:
            return value.replace(
                tzinfo=timezone.utc
            )

        return value.astimezone(
            timezone.utc
        )

    if isinstance(
        value,
        (int, float),
    ):
        try:
            return datetime.fromtimestamp(
                float(value),
                tz=timezone.utc,
            )
        except Exception:
            return None

    text = str(
        value
    ).strip()

    if not text:
        return None

    try:
        return datetime.fromtimestamp(
            float(text),
            tz=timezone.utc,
        )
    except Exception:
        pass

    try:
        parsed = datetime.fromisoformat(
            text.replace(
                "Z",
                "+00:00",
            )
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed.astimezone(
            timezone.utc
        )

    except Exception:
        return None


def bar_timestamp_utc(
    bar,
) -> datetime | None:
    """
    Return the UTC start timestamp for a REST or internally
    constructed LIVE bar.

    LIVE bars normally expose both:
    - timestamp
    - bucket_start

    REST bars normally expose:
    - timestamp
    """
    if bar is None:
        return None

    raw_timestamp = (
        _bar_field(
            bar,
            "timestamp",
        )
        or _bar_field(
            bar,
            "t",
        )
    )

    parsed = _timestamp_utc(
        raw_timestamp
    )

    if parsed is not None:
        return parsed

    return _timestamp_utc(
        _bar_field(
            bar,
            "bucket_start",
        )
    )


def _bar_end_timestamp_utc(
    bar,
    *,
    timeframe_seconds: int,
) -> datetime | None:
    explicit_end = _timestamp_utc(
        _bar_field(
            bar,
            "bucket_end",
        )
    )

    if explicit_end is not None:
        return explicit_end

    start = bar_timestamp_utc(
        bar
    )

    if start is None:
        return None

    return start + timedelta(
        seconds=max(
            1,
            int(
                timeframe_seconds
                or 1
            ),
        )
    )


def _optional_float(
    value,
) -> float | None:
    if value in (
        None,
        "",
    ):
        return None

    try:
        return float(
            value
        )
    except Exception:
        return None


def _rounded_optional(
    value,
    digits: int = 8,
):
    value = _optional_float(
        value
    )

    if value is None:
        return None

    return round(
        value,
        digits,
    )


def _numeric_delta(
    live_value,
    rest_value,
):
    live_number = _optional_float(
        live_value
    )

    rest_number = _optional_float(
        rest_value
    )

    if (
        live_number is None
        or rest_number is None
    ):
        return None

    return round(
        live_number
        - rest_number,
        8,
    )


def _numeric_pct_delta(
    live_value,
    rest_value,
):
    live_number = _optional_float(
        live_value
    )

    rest_number = _optional_float(
        rest_value
    )

    if (
        live_number is None
        or rest_number is None
        or rest_number == 0
    ):
        return None

    return round(
        (
            live_number
            - rest_number
        )
        / rest_number,
        8,
    )


def _numeric_ratio(
    numerator,
    denominator,
):
    numerator_number = _optional_float(
        numerator
    )

    denominator_number = _optional_float(
        denominator
    )

    if (
        numerator_number is None
        or denominator_number is None
        or denominator_number == 0
    ):
        return None

    return round(
        numerator_number
        / denominator_number,
        8,
    )


def _iso_or_none(
    value,
):
    parsed = _timestamp_utc(
        value
    )

    return (
        parsed.isoformat()
        if parsed is not None
        else None
    )


def build_exact_rest_live_bar_comparison(
    *,
    rest_bar,
    live_bars,
    timeframe_seconds: int = 300,
) -> dict:
    """
    Match one REST bar to the internally constructed LIVE bar
    with the exact same UTC bucket-start timestamp.

    This function does not choose the latest LIVE bar. It compares
    equivalent five-minute market intervals only.
    """
    timeframe_seconds = max(
        1,
        int(
            timeframe_seconds
            or 300
        ),
    )

    result = {
        "bar_match_status": (
            "missing_rest_bar"
        ),
        "bar_match_exact": False,
        "bar_match_comparison_eligible": (
            False
        ),

        "bar_match_rest_timestamp": None,
        "bar_match_rest_end_timestamp": (
            None
        ),

        "bar_match_live_timestamp": None,
        "bar_match_live_end_timestamp": (
            None
        ),

        "bar_match_nearest_live_timestamp": (
            None
        ),
        "bar_match_nearest_live_delta_seconds": (
            None
        ),

        "bar_match_live_sealed": None,
        "bar_match_live_sealed_at": None,
        "bar_match_live_capture_quality": (
            None
        ),
        "bar_match_live_full_capture_candidate": (
            False
        ),
        "bar_match_live_late_created_after_seal": (
            None
        ),

        "bar_match_live_event_timestamp_fallback_count": (
            None
        ),
        "bar_match_live_trade_id_missing_count": (
            None
        ),
        "bar_match_live_duplicate_trade_message_count": (
            None
        ),
        "bar_match_live_late_after_seal_trade_count": (
            None
        ),
        "bar_match_live_late_after_seal_volume": (
            None
        ),

        "rest_open": None,
        "rest_high": None,
        "rest_low": None,
        "rest_close": None,
        "rest_volume": None,
        "rest_trade_count": None,
        "rest_vwap": None,

        "matched_live_open": None,
        "matched_live_high": None,
        "matched_live_low": None,
        "matched_live_close": None,
        "matched_live_volume": None,
        "matched_live_trade_count": None,
        "matched_live_vwap": None,

        "open_delta_live_minus_rest": None,
        "high_delta_live_minus_rest": None,
        "low_delta_live_minus_rest": None,
        "close_delta_live_minus_rest": None,
        "volume_delta_live_minus_rest": None,
        "trade_count_delta_live_minus_rest": (
            None
        ),
        "vwap_delta_live_minus_rest": None,

        "open_pct_delta_live_minus_rest": (
            None
        ),
        "high_pct_delta_live_minus_rest": (
            None
        ),
        "low_pct_delta_live_minus_rest": (
            None
        ),
        "close_pct_delta_live_minus_rest": (
            None
        ),
        "volume_pct_delta_live_minus_rest": (
            None
        ),
        "trade_count_pct_delta_live_minus_rest": (
            None
        ),
        "vwap_pct_delta_live_minus_rest": (
            None
        ),

        "volume_capture_ratio_live_to_rest": (
            None
        ),
        "trade_count_capture_ratio_live_to_rest": (
            None
        ),
    }

    if rest_bar is None:
        return result

    rest_timestamp = bar_timestamp_utc(
        rest_bar
    )

    if rest_timestamp is None:
        result[
            "bar_match_status"
        ] = "missing_rest_timestamp"

        return result

    rest_end_timestamp = (
        _bar_end_timestamp_utc(
            rest_bar,
            timeframe_seconds=(
                timeframe_seconds
            ),
        )
    )

    result.update({
        "bar_match_rest_timestamp": (
            rest_timestamp.isoformat()
        ),
        "bar_match_rest_end_timestamp": (
            rest_end_timestamp.isoformat()
            if rest_end_timestamp
            else None
        ),

        "rest_open": _rounded_optional(
            _bar_field(
                rest_bar,
                "open",
            )
        ),
        "rest_high": _rounded_optional(
            _bar_field(
                rest_bar,
                "high",
            )
        ),
        "rest_low": _rounded_optional(
            _bar_field(
                rest_bar,
                "low",
            )
        ),
        "rest_close": _rounded_optional(
            _bar_field(
                rest_bar,
                "close",
            )
        ),
        "rest_volume": _rounded_optional(
            _bar_field(
                rest_bar,
                "volume",
            )
        ),
        "rest_trade_count": _rounded_optional(
            _bar_field(
                rest_bar,
                "trade_count",
            )
        ),
        "rest_vwap": _rounded_optional(
            _bar_field(
                rest_bar,
                "vwap",
            )
        ),
    })

    parsed_live_bars = []

    for live_bar in list(
        live_bars or []
    ):
        live_timestamp = (
            bar_timestamp_utc(
                live_bar
            )
        )

        if live_timestamp is None:
            continue

        parsed_live_bars.append((
            live_timestamp,
            live_bar,
        ))

    if not parsed_live_bars:
        result[
            "bar_match_status"
        ] = "missing_live_5m_bars"

        return result

    parsed_live_bars.sort(
        key=lambda item: item[0]
    )

    exact_live_bar = None

    for (
        live_timestamp,
        live_bar,
    ) in parsed_live_bars:
        timestamp_delta = (
            live_timestamp
            - rest_timestamp
        ).total_seconds()

        if abs(
            timestamp_delta
        ) <= 0.000001:
            exact_live_bar = live_bar
            break

    if exact_live_bar is None:
        nearest_timestamp, _ = min(
            parsed_live_bars,
            key=lambda item: abs(
                (
                    item[0]
                    - rest_timestamp
                ).total_seconds()
            ),
        )

        nearest_delta_seconds = (
            nearest_timestamp
            - rest_timestamp
        ).total_seconds()

        result.update({
            "bar_match_nearest_live_timestamp": (
                nearest_timestamp.isoformat()
            ),
            "bar_match_nearest_live_delta_seconds": round(
                nearest_delta_seconds,
                6,
            ),
        })

        earliest_timestamp = (
            parsed_live_bars[0][0]
        )

        latest_timestamp = (
            parsed_live_bars[-1][0]
        )

        if rest_timestamp < earliest_timestamp:
            status = (
                "rest_bar_before_live_history"
            )

        elif rest_timestamp > latest_timestamp:
            status = (
                "live_bar_not_yet_built"
            )

        else:
            status = (
                "no_exact_timestamp_match"
            )

        result[
            "bar_match_status"
        ] = status

        return result

    live_timestamp = bar_timestamp_utc(
        exact_live_bar
    )

    live_end_timestamp = (
        _bar_end_timestamp_utc(
            exact_live_bar,
            timeframe_seconds=(
                timeframe_seconds
            ),
        )
    )

    live_sealed = bool(
        _bar_field(
            exact_live_bar,
            "sealed",
            False,
        )
    )

    capture_quality = str(
        _bar_field(
            exact_live_bar,
            "capture_quality",
            "",
        )
        or ""
    ).strip()

    result.update({
        "bar_match_status": (
            "matched"
            if live_sealed
            else
            "matching_live_bar_not_sealed"
        ),
        "bar_match_exact": True,
        "bar_match_comparison_eligible": (
            live_sealed
        ),

        "bar_match_live_timestamp": (
            live_timestamp.isoformat()
            if live_timestamp
            else None
        ),
        "bar_match_live_end_timestamp": (
            live_end_timestamp.isoformat()
            if live_end_timestamp
            else None
        ),

        "bar_match_live_sealed": (
            live_sealed
        ),
        "bar_match_live_sealed_at": (
            _iso_or_none(
                _bar_field(
                    exact_live_bar,
                    "sealed_at",
                )
                or _bar_field(
                    exact_live_bar,
                    "sealed_at_epoch",
                )
            )
        ),
        "bar_match_live_capture_quality": (
            capture_quality
            or None
        ),
        "bar_match_live_full_capture_candidate": (
            capture_quality
            == "FULL_CAPTURE_CANDIDATE"
        ),
        "bar_match_live_late_created_after_seal": bool(
            _bar_field(
                exact_live_bar,
                "late_created_after_seal",
                False,
            )
        ),

        "bar_match_live_event_timestamp_fallback_count": (
            _bar_field(
                exact_live_bar,
                "event_timestamp_fallback_count",
            )
        ),
        "bar_match_live_trade_id_missing_count": (
            _bar_field(
                exact_live_bar,
                "trade_id_missing_count",
            )
        ),
        "bar_match_live_duplicate_trade_message_count": (
            _bar_field(
                exact_live_bar,
                "duplicate_trade_message_count",
            )
        ),
        "bar_match_live_late_after_seal_trade_count": (
            _bar_field(
                exact_live_bar,
                "late_after_seal_trade_count",
            )
        ),
        "bar_match_live_late_after_seal_volume": (
            _rounded_optional(
                _bar_field(
                    exact_live_bar,
                    "late_after_seal_volume",
                )
            )
        ),

        "matched_live_open": _rounded_optional(
            _bar_field(
                exact_live_bar,
                "open",
            )
        ),
        "matched_live_high": _rounded_optional(
            _bar_field(
                exact_live_bar,
                "high",
            )
        ),
        "matched_live_low": _rounded_optional(
            _bar_field(
                exact_live_bar,
                "low",
            )
        ),
        "matched_live_close": _rounded_optional(
            _bar_field(
                exact_live_bar,
                "close",
            )
        ),
        "matched_live_volume": _rounded_optional(
            _bar_field(
                exact_live_bar,
                "volume",
            )
        ),
        "matched_live_trade_count": (
            _rounded_optional(
                _bar_field(
                    exact_live_bar,
                    "trade_count",
                )
            )
        ),
        "matched_live_vwap": _rounded_optional(
            _bar_field(
                exact_live_bar,
                "vwap",
            )
        ),
    })

    comparison_fields = (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trade_count",
        "vwap",
    )

    for field in comparison_fields:
        rest_value = result.get(
            f"rest_{field}"
        )

        live_value = result.get(
            f"matched_live_{field}"
        )

        result[
            f"{field}_delta_live_minus_rest"
        ] = _numeric_delta(
            live_value,
            rest_value,
        )

        result[
            f"{field}_pct_delta_live_minus_rest"
        ] = _numeric_pct_delta(
            live_value,
            rest_value,
        )

    result[
        "volume_capture_ratio_live_to_rest"
    ] = _numeric_ratio(
        result.get(
            "matched_live_volume"
        ),
        result.get(
            "rest_volume"
        ),
    )

    result[
        "trade_count_capture_ratio_live_to_rest"
    ] = _numeric_ratio(
        result.get(
            "matched_live_trade_count"
        ),
        result.get(
            "rest_trade_count"
        ),
    )

    return result


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
    Fetch recent bars, expanding the calendar lookback until enough symbols
    have enough actual market bars.

    This avoids Monday/holiday/off-hours problems where a short calendar
    lookback may contain too few trading bars.
    """
    if not symbols:
        return {}

    symbols = list(symbols)

    try:
        required_ready_symbols = (
            len(symbols)
            if min_ready_symbols is None
            else int(min_ready_symbols)
        )
    except Exception:
        required_ready_symbols = len(symbols)

    required_ready_symbols = max(
        1,
        min(len(symbols), required_ready_symbols),
    )

    lookback_hours = initial_lookback_hours
    latest_result = {}
    latest_bar_counts = {}
    latest_symbols_with_enough_bars = []

    while lookback_hours <= max_lookback_hours:
        latest_result = fetch_recent_bars(
            data_client,
            symbols,
            lookback_hours=lookback_hours,
            timeframe_minutes=timeframe_minutes,
        )

        latest_bar_counts = {
            symbol: len(latest_result.get(symbol, []))
            for symbol in symbols
        }

        latest_symbols_with_enough_bars = [
            symbol
            for symbol, count in latest_bar_counts.items()
            if count >= min_bars
        ]

        logging.info(
            "[Bars] Min-count check | lookback_hours=%s min_bars=%s "
            "symbols_ready=%s/%s required_ready_symbols=%s bar_counts=%s",
            lookback_hours,
            min_bars,
            len(latest_symbols_with_enough_bars),
            len(symbols),
            required_ready_symbols,
            latest_bar_counts,
        )

        if len(latest_symbols_with_enough_bars) >= required_ready_symbols:
            return latest_result

        lookback_hours *= 2

    logging.warning(
        "[Bars] Could not reach min_bars=%s for required_ready_symbols=%s "
        "within max_lookback_hours=%s. Returning latest result anyway. "
        "symbols_ready=%s/%s bar_counts=%s",
        min_bars,
        required_ready_symbols,
        max_lookback_hours,
        len(latest_symbols_with_enough_bars),
        len(symbols),
        latest_bar_counts,
    )

    return latest_result