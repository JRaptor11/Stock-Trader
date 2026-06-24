import logging
from datetime import datetime, timezone

from state import app_state


IGNORED_TARGET_KEYS = {"CASH", "_meta"}


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def clean_target_portfolio(target: dict) -> tuple[dict, float, dict]:
    """
    Split Layer 2 target output into:
    - tradable symbol weights
    - target cash percentage
    - metadata

    Layer 3 should never try to trade CASH or _meta.
    """
    if not isinstance(target, dict):
        return {}, 1.0, {}

    target_weights = {}

    for symbol, weight in target.items():
        symbol = str(symbol).upper().strip()

        if symbol in IGNORED_TARGET_KEYS:
            continue

        weight = _safe_float(weight, 0.0)

        if weight <= 0:
            continue

        target_weights[symbol] = weight

    target_cash_pct = _safe_float(target.get("CASH", 0.0), 0.0)

    target_meta = target.get("_meta", {})
    if not isinstance(target_meta, dict):
        target_meta = {}

    return target_weights, target_cash_pct, target_meta


def run_layer3_dry_run() -> dict:
    """
    First Layer 3 integration point.

    This does not place orders.
    It only reads the latest Layer 1/2 target and stores a basic dry-run snapshot.
    """
    layers = app_state.setdefault("layers", {})
    latest = layers.get("latest", {})
    rebalance = layers.setdefault("rebalance", {})

    if not rebalance.get("enabled", True):
        summary = {
            "status": "disabled",
            "reason": "Layer 3 rebalance is disabled",
        }
        rebalance["last_summary"] = summary
        return summary

    target = latest.get("target_portfolio", {})

    target_weights, target_cash_pct, target_meta = clean_target_portfolio(target)

    cycle_id = int(rebalance.get("last_cycle_id", 0) or 0) + 1
    timestamp = datetime.now(timezone.utc).isoformat()

    # For now, this is just a clean integration snapshot.
    # The next step will replace this placeholder with real drift calculations.
    plan = []

    for symbol, target_weight in target_weights.items():
        plan.append({
            "cycle_id": cycle_id,
            "timestamp": timestamp,
            "symbol": symbol,
            "target_weight": target_weight,
            "decision": "OBSERVE",
            "reason": "Layer 3 dry-run skeleton active; broker drift planning not added yet.",
        })

    summary = {
        "status": "ok",
        "dry_run": True,
        "cycle_id": cycle_id,
        "timestamp": timestamp,
        "target_symbol_count": len(target_weights),
        "target_cash_pct": target_cash_pct,
        "market_strength": target_meta.get("market_strength"),
        "plan_count": len(plan),
    }

    rebalance["last_cycle_id"] = cycle_id
    rebalance["last_run_at"] = timestamp
    rebalance["last_plan"] = plan
    rebalance["last_summary"] = summary
    rebalance["last_error"] = None

    logging.info(
        "[Layer3] Dry-run skeleton complete | cycle_id=%s target_symbols=%s cash=%.2f%%",
        cycle_id,
        list(target_weights.keys()),
        target_cash_pct * 100,
    )

    return summary