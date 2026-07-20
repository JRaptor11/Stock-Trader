# layers/layer_bar_gate.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _bar_value(bar: Any, key: str, default=None):
    if isinstance(bar, dict):
        return bar.get(key, default)

    return getattr(bar, key, default)


def _to_aware_utc(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except Exception:
        return None


def latest_bar_timestamp(bar: Any) -> datetime | None:
    """
    Return an aware UTC timestamp from a REST or locally built bar.
    """
    return _to_aware_utc(
        _bar_value(bar, "timestamp")
        or _bar_value(bar, "t")
    )


def build_distinct_bar_report(
    *,
    source: str,
    bars_by_symbol: dict,
    symbols: list[str],
    last_accepted_timestamp_by_symbol: dict | None,
    required_new_symbols: int,
) -> dict:
    """
    Determine whether a new common bar cohort is available.

    The candidate cohort is the newest latest-bar timestamp observed across
    the supplied symbols.

    A symbol counts as new only when:
    - its latest bar belongs to that newest cohort
    - its timestamp is later than the last accepted timestamp for the symbol

    This prevents duplicate fetches or late catch-up bars from advancing the
    strategic cycle by themselves.
    """
    source = str(source or "UNKNOWN").upper().strip()
    previous_map = last_accepted_timestamp_by_symbol or {}

    latest_dt_by_symbol: dict[str, datetime] = {}
    previous_dt_by_symbol: dict[str, datetime | None] = {}
    missing_symbols: list[str] = []

    for raw_symbol in symbols or []:
        symbol = str(raw_symbol or "").upper().strip()

        if not symbol:
            continue

        bars = list(
            bars_by_symbol.get(symbol, []) or []
        )

        latest_dt = (
            latest_bar_timestamp(bars[-1])
            if bars
            else None
        )

        previous_dt = _to_aware_utc(
            previous_map.get(symbol)
        )

        previous_dt_by_symbol[symbol] = previous_dt

        if latest_dt is None:
            missing_symbols.append(symbol)
            continue

        latest_dt_by_symbol[symbol] = latest_dt

    candidate_dt = (
        max(latest_dt_by_symbol.values())
        if latest_dt_by_symbol
        else None
    )

    new_symbols: list[str] = []
    duplicate_symbols: list[str] = []
    lagging_symbols: list[str] = []

    for symbol, latest_dt in latest_dt_by_symbol.items():
        previous_dt = previous_dt_by_symbol.get(symbol)

        if (
            candidate_dt is not None
            and latest_dt < candidate_dt
        ):
            lagging_symbols.append(symbol)
            continue

        if previous_dt is None or latest_dt > previous_dt:
            new_symbols.append(symbol)
        else:
            duplicate_symbols.append(symbol)

    required_new_symbols = max(
        1,
        int(required_new_symbols or 1),
    )

    return {
        "source": source,

        "candidate_bar_timestamp": (
            candidate_dt.isoformat()
            if candidate_dt is not None
            else None
        ),

        "latest_timestamp_by_symbol": {
            symbol: value.isoformat()
            for symbol, value
            in latest_dt_by_symbol.items()
        },

        "previous_accepted_timestamp_by_symbol": {
            symbol: (
                value.isoformat()
                if value is not None
                else None
            )
            for symbol, value
            in previous_dt_by_symbol.items()
        },

        "new_symbols": sorted(new_symbols),
        "new_symbol_count": len(new_symbols),
        "required_new_symbols": required_new_symbols,

        "duplicate_symbols": sorted(
            duplicate_symbols
        ),
        "lagging_symbols": sorted(
            lagging_symbols
        ),
        "missing_symbols": sorted(
            missing_symbols
        ),

        "ready": (
            len(new_symbols)
            >= required_new_symbols
        ),
    }


def accept_distinct_bar_report(
    gate_state: dict,
    report: dict,
    *,
    cycle_id=None,
    accepted_reason: str | None = None,
) -> None:
    """
    Persist the bar snapshot only after a strategic cycle is accepted.
    """
    accepted = gate_state.setdefault(
        "last_accepted_timestamp_by_symbol",
        {},
    )

    for symbol, timestamp in (
        report.get("latest_timestamp_by_symbol")
        or {}
    ).items():
        if timestamp:
            accepted[symbol] = timestamp

    gate_state[
        "last_accepted_candidate_bar_timestamp"
    ] = report.get("candidate_bar_timestamp")

    gate_state["last_accepted_cycle_id"] = cycle_id
    gate_state["last_accepted_reason"] = (
        accepted_reason
    )
    gate_state["last_accepted_at"] = (
        datetime.now(timezone.utc).isoformat()
    )
    gate_state["last_accepted_report"] = dict(
        report
    )