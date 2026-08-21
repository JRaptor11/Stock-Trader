from __future__ import annotations

from datetime import datetime, timezone

from utils.numeric import safe_float, safe_int


def _value(bar, name: str):
    return bar.get(name) if isinstance(bar, dict) else getattr(bar, name, None)


def latest_completed_live_cohort(
    bars_by_symbol: dict, *, timeframe_seconds: int, required_symbols: int,
    expected_symbols: list[str] | None = None,
) -> dict:
    """Return the newest local-live bucket shared by the required universe."""
    required_symbols = max(1, safe_int(required_symbols, 1))
    count_by_epoch: dict[float, int] = {}
    symbols_by_epoch: dict[float, list[str]] = {}
    for raw_symbol, bars in (bars_by_symbol or {}).items():
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol:
            continue
        symbol_epochs = {
            float(bucket_start)
            for bar in (bars or [])
            if (bucket_start := safe_float(_value(bar, "bucket_start"), 0.0)) > 0
        }
        for bucket_start in symbol_epochs:
            count_by_epoch[bucket_start] = count_by_epoch.get(bucket_start, 0) + 1
            symbols_by_epoch.setdefault(bucket_start, []).append(symbol)

    expected = {
        str(symbol or "").upper().strip()
        for symbol in (expected_symbols or bars_by_symbol.keys())
        if str(symbol or "").strip()
    }
    eligible = [epoch for epoch, count in count_by_epoch.items() if count >= required_symbols]
    if not eligible:
        return {
            "status": "waiting_for_completed_live_cohort",
            "required_symbol_count": required_symbols,
            "expected_symbol_count": len(expected),
            "full_universe_parity": False,
            "available_cohort_counts": {
                datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(): count
                for epoch, count in sorted(count_by_epoch.items())
            },
        }
    start = max(eligible)
    end = start + max(1, safe_int(timeframe_seconds, 300))
    symbols = sorted(symbols_by_epoch.get(start, []))
    missing = sorted(expected - set(symbols))
    return {
        "status": "ready", "bucket_start_epoch": start,
        "bucket_end_epoch": end,
        "bucket_start_timestamp": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
        "bucket_end_timestamp": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(),
        "symbol_count": len(symbols), "symbols": symbols,
        "required_symbol_count": required_symbols,
        "expected_symbol_count": len(expected),
        "missing_symbols": missing,
        "coverage_pct": (len(set(symbols) & expected) / len(expected) * 100.0) if expected else 100.0,
        "full_universe_parity": not missing,
    }
