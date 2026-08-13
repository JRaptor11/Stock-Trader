from __future__ import annotations

from utils.numeric import safe_float


def update_layer4_shadow_outcome(
    item: dict, *, current_price: float, now_epoch: float,
    now_iso: str, market_is_open: bool,
) -> tuple[dict | None, bool]:
    """Update horizon marks; return (final row, should_remain_pending)."""
    created = safe_float(item.get("created_epoch"), 0.0)
    start = safe_float(item.get("start_live_price"), 0.0)
    current = safe_float(current_price, 0.0)
    if created <= 0 or start <= 0 or current <= 0:
        return None, True
    age = now_epoch - created
    for seconds, key in ((600, "10m"), (1800, "30m"), (3600, "60m")):
        field = f"forward_return_{key}"
        if age >= seconds and field not in item:
            item[field] = round((current - start) / start, 6)
    complete = all(f"forward_return_{key}" in item for key in ("10m", "30m", "60m"))
    if complete:
        reason = "all_horizons_complete"
    elif not market_is_open and age >= 600:
        reason = "market_closed_partial"
    else:
        return None, True
    qty = safe_float(item.get("original_qty"), 0.0)

    def avoided(key: str):
        value = item.get(f"forward_return_{key}")
        return None if value is None else round(-safe_float(value, 0.0) * start * qty, 2)

    return ({
        **item, "outcome_timestamp": now_iso, "outcome_live_price": current,
        "avoided_pnl_10m": avoided("10m"), "avoided_pnl_30m": avoided("30m"),
        "avoided_pnl_60m": avoided("60m"),
        "avoided_pnl_to_outcome": round((start - current) * qty, 2),
        "finalized_reason": reason,
    }, False)
