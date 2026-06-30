from utils.numeric import safe_round


def compact_order_for_log(order: dict) -> dict:
    compact = {
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "status": order.get("status"),
        "qty": safe_round(order.get("qty"), 4),
        "notional": safe_round(order.get("notional"), 2),
        "price": safe_round(order.get("price"), 4),
        "order_id": order.get("order_id"),
        "reason": order.get("reason"),
        "row_id": order.get("row_id"),
    }

    if order.get("error"):
        compact["error"] = order.get("error")

    if order.get("cash") is not None:
        compact["cash"] = safe_round(order.get("cash"), 2)

    return {k: v for k, v in compact.items() if v is not None}


def compact_orders_for_log(orders) -> list[dict]:
    return [
        compact_order_for_log(order)
        for order in orders or []
        if isinstance(order, dict)
    ]


def compact_executable_row_for_log(row: dict) -> dict:
    return {
        "symbol": row.get("symbol"),
        "decision": row.get("decision"),
        "qty": safe_round(row.get("qty"), 4),
        "notional": safe_round(row.get("notional"), 2),
        "price": safe_round(row.get("price"), 4),
        "reason": row.get("reason"),
    }


def target_summary_for_log(target: dict) -> dict:
    if not isinstance(target, dict):
        return {}

    meta = target.get("_meta", {}) if isinstance(target.get("_meta"), dict) else {}

    weights = {}
    for key, value in target.items():
        if key == "_meta":
            continue
        try:
            weights[key] = round(float(value), 4)
        except Exception:
            weights[key] = value

    return {
        "weights": weights,
        "market_strength": meta.get("market_strength"),
        "cash_pct": meta.get("cash_pct", weights.get("CASH")),
        "investable_pct": meta.get("investable_pct"),
        "top_score": meta.get("top_score"),
        "avg_top_score": meta.get("avg_top_score"),
        "smoothing_applied": meta.get("smoothing_applied"),
        "smoothing_mode": meta.get("smoothing_mode"),
    }