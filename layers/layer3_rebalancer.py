import logging
from datetime import datetime, timezone, timedelta

from core.market_clock import get_market_is_open
from utils.numeric import safe_float, safe_int, safe_round
from utils.symbols import normalize_symbol

from core.state import app_state
from config import runtime_config as config

try:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
except Exception:
    QueryOrderStatus = None
    GetOrdersRequest = None


L3_MAX_TRADES_PER_CYCLE = 6
L3_MAX_BUYS_PER_CYCLE = 3
L3_MAX_SELLS_PER_CYCLE = 3

L3_MAX_TOTAL_BUY_NOTIONAL_PER_CYCLE = 22500.0
L3_MAX_BUY_NOTIONAL_PER_CYCLE = 7500.0
L3_MAX_SELL_NOTIONAL_PER_CYCLE = 7500.0

L3_MAX_NEW_POSITION_WEIGHT_PER_CYCLE = 0.075
L3_MAX_ADD_WEIGHT_PER_CYCLE = 0.075
L3_MAX_TRIM_WEIGHT_PER_CYCLE = 0.075         # Max 7.5% equity trimmed per cycle

IGNORED_TARGET_KEYS = {"CASH", "_META"}

# Layer 3 dry-run planning thresholds.
# Keep these local for now. We can move them into constants.py after the planner proves itself.
L3_MIN_TRADE_VALUE_DOLLARS = 25.0
L3_MIN_ABS_WEIGHT_DRIFT = 0.025          # 2.5 percentage points
L3_MIN_RELATIVE_DRIFT = 0.25             # 25% relative drift from target/current value
L3_REQUIRE_TARGET_CONFIRMATION_CYCLES = 2
L3_REQUIRE_EXIT_CONFIRMATION_CYCLES = 2
L3_WHOLE_SHARES_ONLY = True
L3_PLAN_TTL_SECONDS = 600


def _norm_symbol(symbol) -> str:
    return str(symbol or "").upper().strip()


def _layer3_bool_setting(name: str, default: bool) -> bool:
    return bool(
        app_state.get("execution", {}).get(
            name,
            getattr(config, name.upper(), default),
        )
    )


def _layer3_int_setting(name: str, default: int) -> int:
    return safe_int(
        app_state.get("execution", {}).get(
            name,
            getattr(config, name.upper(), default),
        ),
        default,
    )


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

    for raw_symbol, raw_weight in target.items():
        symbol = _norm_symbol(raw_symbol)

        if symbol in IGNORED_TARGET_KEYS:
            continue

        weight = safe_float(raw_weight, 0.0)

        if weight <= 0:
            continue

        target_weights[symbol] = weight

    target_cash_pct = safe_float(target.get("CASH", 0.0), 0.0)

    target_meta = target.get("_meta", {})
    if not isinstance(target_meta, dict):
        target_meta = {}

    return target_weights, target_cash_pct, target_meta


def _ranked_price_map(latest: dict) -> dict:
    """
    Build symbol -> last_price from the stored Layer 1 ranked snapshot.

    For now, this is the cleanest price source because Layer 1/2 already used
    recent bar data to produce the target. Later we can upgrade this to live quote/trade prices.
    """
    prices = {}

    for row in latest.get("ranked", []) or []:
        if not isinstance(row, dict):
            continue

        symbol = _norm_symbol(row.get("symbol"))
        price = safe_float(row.get("last_price"), 0.0)

        if symbol and price > 0:
            prices[symbol] = price

    return prices


def _get_account_snapshot() -> dict:
    """
    Get account equity/cash/buying power.

    Prefer live Alpaca account data.
    Fall back to the balance tracker if needed.
    """
    client = app_state.get("trading_client")

    if client:
        try:
            account = client.get_account()

            return {
                "source": "alpaca_account",
                "equity": safe_float(getattr(account, "equity", 0.0), 0.0),
                "cash": safe_float(getattr(account, "cash", 0.0), 0.0),
                "buying_power": safe_float(getattr(account, "buying_power", 0.0), 0.0),
            }

        except Exception:
            logging.warning("[Layer3] Failed to fetch Alpaca account snapshot.", exc_info=True)

    try:
        tracker = app_state.get("services", {}).get("balance_tracker", {}).get("instance")
        if tracker and hasattr(tracker, "get_balance"):
            balance = tracker.get_balance()
            return {
                "source": "balance_tracker",
                "equity": safe_float(balance.get("equity"), 0.0),
                "cash": safe_float(balance.get("balance"), 0.0),
                "buying_power": safe_float(balance.get("balance"), 0.0),
            }
    except Exception:
        logging.warning("[Layer3] Failed to use balance tracker fallback.", exc_info=True)

    balance_state = app_state.get("services", {}).get("balance_tracker", {})
    return {
        "source": "app_state_balance_tracker",
        "equity": safe_float(balance_state.get("equity"), 0.0),
        "cash": safe_float(balance_state.get("balance"), 0.0),
        "buying_power": safe_float(balance_state.get("balance"), 0.0),
    }


def _get_positions_snapshot() -> dict:
    """
    Get broker positions as symbol -> normalized position snapshot.

    Layer 3 should treat broker state as the source of truth.
    """
    client = app_state.get("trading_client")
    positions_by_symbol = {}

    if not client:
        logging.warning("[Layer3] No trading_client available for position snapshot.")
        return positions_by_symbol

    try:
        positions = client.get_all_positions()
    except Exception:
        logging.warning("[Layer3] Failed to fetch Alpaca positions.", exc_info=True)
        return positions_by_symbol

    for pos in positions:
        symbol = _norm_symbol(getattr(pos, "symbol", ""))
        qty = safe_float(getattr(pos, "qty", 0.0), 0.0)

        if not symbol or qty <= 0:
            continue

        avg_entry_price = safe_float(getattr(pos, "avg_entry_price", 0.0), 0.0)
        current_price = safe_float(getattr(pos, "current_price", 0.0), 0.0)
        market_value = safe_float(getattr(pos, "market_value", 0.0), 0.0)
        unrealized_plpc = safe_float(getattr(pos, "unrealized_plpc", 0.0), 0.0)

        if current_price <= 0 and qty > 0 and market_value > 0:
            current_price = market_value / qty

        if current_price <= 0:
            current_price = avg_entry_price

        if market_value <= 0 and current_price > 0:
            market_value = qty * current_price

        positions_by_symbol[symbol] = {
            "symbol": symbol,
            "qty": qty,
            "avg_entry_price": avg_entry_price,
            "current_price": current_price,
            "market_value": market_value,
            "unrealized_plpc": unrealized_plpc,
        }

    return positions_by_symbol


def _get_open_order_symbols() -> tuple[set, dict]:
    """
    Return symbols with open orders, using both local tracked open_orders
    and broker-side open orders when available.
    """
    symbols = set()
    details = {}

    for raw_symbol, tracked in app_state.setdefault("open_orders", {}).items():
        symbol = _norm_symbol(raw_symbol)
        if not symbol:
            continue

        symbols.add(symbol)
        details[symbol] = {
            "source": "local_open_orders",
            "data": tracked,
        }

    client = app_state.get("trading_client")

    if client and QueryOrderStatus is not None and GetOrdersRequest is not None:
        try:
            params = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            open_orders = client.get_orders(filter=params)

            for order in open_orders:
                symbol = _norm_symbol(getattr(order, "symbol", ""))
                if not symbol:
                    continue

                symbols.add(symbol)
                details[symbol] = {
                    "source": "broker_open_orders",
                    "order_id": str(getattr(order, "id", "")),
                    "side": str(getattr(order, "side", "")),
                    "status": str(getattr(order, "status", "")),
                }

        except Exception:
            logging.warning("[Layer3] Failed to fetch broker open orders.", exc_info=True)

    return symbols, details


def _update_target_stability(
    rebalance: dict,
    target_weights: dict,
    positions: dict,
) -> tuple[dict, dict]:
    """
    Track how many consecutive cycles a symbol has appeared in the target,
    and how many consecutive cycles a held symbol has been absent from the target.

    If LAYER3_MARKET_HOURS_ONLY=true, confirmation counters only advance
    while the market is open. Layer 3 may still plan/log after hours,
    but it should not become more confident from closed-market cycles.
    """
    seen_counts = rebalance.setdefault("target_seen_counts", {})
    absent_counts = rebalance.setdefault("target_absent_counts", {})

    target_symbols = set(target_weights.keys())
    all_symbols = set(target_symbols) | set(positions.keys())

    market_is_open = get_market_is_open(app_state)
    market_hours_only = _layer3_bool_setting(
        "layer3_market_hours_only",
        True,
    )

    confirmation_updates_allowed = (
        market_is_open or not market_hours_only
    )

    rebalance["market_is_open"] = market_is_open
    rebalance["confirmation_updates_allowed"] = confirmation_updates_allowed
    rebalance["confirmation_updates_blocked_reason"] = (
        None if confirmation_updates_allowed else "market_closed"
    )

    if not confirmation_updates_allowed:
        logging.info(
            "[Layer3] Confirmation counters frozen because market is closed "
            "and LAYER3_MARKET_HOURS_ONLY=true."
        )
        return seen_counts, absent_counts

    for symbol in all_symbols:
        if symbol in target_symbols:
            seen_counts[symbol] = safe_int(seen_counts.get(symbol, 0), 0) + 1
            absent_counts[symbol] = 0
        else:
            absent_counts[symbol] = safe_int(absent_counts.get(symbol, 0), 0) + 1
            seen_counts[symbol] = 0

    rebalance["last_confirmation_update_at"] = datetime.now(
        timezone.utc
    ).isoformat()

    # Optional cleanup so these dicts do not grow forever.
    keep_symbols = all_symbols

    for symbol in list(seen_counts.keys()):
        if symbol not in keep_symbols and safe_int(seen_counts.get(symbol, 0), 0) <= 0:
            seen_counts.pop(symbol, None)

    for symbol in list(absent_counts.keys()):
        if symbol not in keep_symbols and safe_int(absent_counts.get(symbol, 0), 0) <= 0:
            absent_counts.pop(symbol, None)

    return seen_counts, absent_counts


def _get_price_for_symbol(symbol: str, ranked_prices: dict, position: dict | None) -> tuple[float, str]:
    """
    Choose the best available price for planning.

    Priority:
    1. Layer 1/2 ranked last_price
    2. Stream last trade price
    3. Broker position current_price
    4. Broker position avg_entry_price
    """
    symbol = _norm_symbol(symbol)

    price = safe_float(ranked_prices.get(symbol), 0.0)
    if price > 0:
        return price, "ranked_last_price"

    price = safe_float(app_state.get("last_trade_price_by_symbol", {}).get(symbol), 0.0)
    if price > 0:
        return price, "last_trade_price_by_symbol"

    if isinstance(position, dict):
        price = safe_float(position.get("current_price"), 0.0)
        if price > 0:
            return price, "position_current_price"

        price = safe_float(position.get("avg_entry_price"), 0.0)
        if price > 0:
            return price, "position_avg_entry_price"

    return 0.0, "missing_price"


def _whole_share_qty(notional: float, price: float) -> float:
    if price <= 0:
        return 0.0

    if L3_WHOLE_SHARES_ONLY:
        return float(int(abs(notional) // price))

    return abs(notional) / price


def _target_qty_from_value(target_value: float, price: float) -> float:
    """
    Convert target dollar value into an ideal target quantity.

    This is the full desired position size, not just the quantity authorized
    for the current execution window.
    """
    if price <= 0:
        return 0.0

    if L3_WHOLE_SHARES_ONLY:
        return float(int(abs(target_value) // price))

    return abs(target_value) / price


def _build_plan_ids(cycle_id: int, created_at_dt: datetime | None = None) -> tuple[str, str, str, int]:
    created_at_dt = created_at_dt or datetime.now(timezone.utc)
    expires_at_dt = created_at_dt + timedelta(seconds=L3_PLAN_TTL_SECONDS)

    created_at = created_at_dt.isoformat()
    expires_at = expires_at_dt.isoformat()
    safe_created = created_at.replace(":", "").replace("+", "Z")
    plan_id = f"L3-{cycle_id}-{safe_created}"

    return plan_id, created_at, expires_at, L3_PLAN_TTL_SECONDS


def _mark_previous_active_execution_plan_replaced(
    layers: dict,
    *,
    new_plan_id: str,
    cycle_id: int,
    timestamp: str,
) -> None:
    previous_active = layers.get("active_execution_plan")

    if not isinstance(previous_active, dict):
        return

    if previous_active.get("status") not in {"active", "working"}:
        return

    previous_active["status"] = "replaced"
    previous_active["replaced_at"] = timestamp
    previous_active["replaced_by_plan_id"] = new_plan_id
    previous_active["replaced_by_cycle_id"] = cycle_id

    history = layers.setdefault("execution_plan_history", [])
    history.append(previous_active)
    del history[:-10]


def _store_active_execution_plan(
    layers: dict,
    *,
    plan_id: str,
    created_at: str,
    expires_at: str,
    ttl_seconds: int,
    summary: dict,
    plan: list[dict],
) -> dict:
    active_plan = {
        "plan_id": plan_id,
        "status": "active",
        "created_at": created_at,
        "expires_at": expires_at,
        "ttl_seconds": ttl_seconds,
        "summary": summary,
        "rows": plan,
    }

    layers["active_execution_plan"] = active_plan
    layers.setdefault("layer4", {})["active_plan_id"] = plan_id
    layers["layer4"]["active_plan_expires_at"] = expires_at

    return active_plan


def _target_qty_from_value(target_value: float, price: float) -> float:
    """
    Convert a target dollar value into a target share quantity.
    """
    if price <= 0:
        return 0.0

    raw_qty = target_value / price

    if L3_WHOLE_SHARES_ONLY:
        return float(int(raw_qty))

    return raw_qty


def _build_row(
    *,
    plan_id,
    row_id,
    plan_created_at,
    plan_expires_at,
    plan_ttl_seconds,
    cycle_id,
    timestamp,
    symbol,
    decision,
    reason,
    target_weight,
    current_weight,
    target_value,
    current_value,
    delta_value,
    delta_weight,
    relative_drift,
    live_price,
    price_source,
    current_qty,
    target_qty,
    qty_delta,
    planned_qty,
    planned_notional,
    target_seen_count,
    target_absent_count,
    open_order_exists,
    open_order_detail,
    blocked_by,
    account,
    target_meta,
):
    planned_qty = round(planned_qty, 6)
    planned_notional = round(planned_notional, 2)

    return {
        "plan_id": plan_id,
        "row_id": row_id,
        "plan_created_at": plan_created_at,
        "plan_expires_at": plan_expires_at,
        "plan_ttl_seconds": int(plan_ttl_seconds),
        "execution_layer": "layer4",

        "cycle_id": cycle_id,
        "timestamp": timestamp,
        "dry_run": True,

        "symbol": symbol,
        "decision": decision,
        "reason": reason,
        "blocked_by": blocked_by,

        "live_price": round(live_price, 4),
        "price_source": price_source,

        "current_qty": round(current_qty, 6),
        "target_qty": round(target_qty, 6),
        "qty_delta": round(qty_delta, 6),

        "current_value": round(current_value, 2),
        "current_weight": round(current_weight, 6),

        "target_weight": round(target_weight, 6),
        "target_value": round(target_value, 2),

        "delta_value": round(delta_value, 2),
        "delta_weight": round(delta_weight, 6),
        "relative_drift": round(relative_drift, 6),

        # Backward-compatible fields used by the old immediate executor.
        "planned_qty": planned_qty,
        "planned_notional": planned_notional,

        # Layer-4-ready aliases. These mean: maximum amount authorized
        # for this plan window, not necessarily amount filled immediately.
        "max_authorized_qty": planned_qty,
        "max_authorized_notional": planned_notional,
        "remaining_authorized_qty": planned_qty,
        "remaining_authorized_notional": planned_notional,
        "filled_qty_so_far": 0.0,
        "filled_notional_so_far": 0.0,

        "target_seen_count": int(target_seen_count or 0),
        "target_absent_count": int(target_absent_count or 0),
        "open_order_exists": bool(open_order_exists),
        "open_order_detail": open_order_detail,
        "open_order_policy": "layer4_reconcile_before_execution",

        "equity": round(account.get("equity", 0.0), 2),
        "cash": round(account.get("cash", 0.0), 2),
        "buying_power": round(account.get("buying_power", 0.0), 2),

        "market_strength": target_meta.get("market_strength"),
    }


def _plan_priority(row):
    decision = row.get("decision")
    symbol = row.get("symbol", "")

    if decision == "SELL":
        return (
            0,
            -abs(safe_float(row.get("delta_value"), 0.0)),
            symbol,
        )

    if decision == "BUY":
        return (
            1,
            -safe_float(row.get("delta_value"), 0.0),
            symbol,
        )

    if decision == "HOLD":
        return (2, symbol)

    return (3, symbol)


def _sync_layer4_authorized_aliases(row: dict) -> dict:
    planned_qty = safe_float(row.get("planned_qty"), 0.0)
    planned_notional = safe_float(row.get("planned_notional"), 0.0)

    row["max_authorized_qty"] = planned_qty
    row["max_authorized_notional"] = planned_notional
    row["remaining_authorized_qty"] = planned_qty
    row["remaining_authorized_notional"] = planned_notional
    return row


def _defer_trade(row: dict, reason: str, blocked_by_reason: str) -> dict:
    row["decision"] = "SKIP"
    row["reason"] = reason
    row["planned_qty"] = 0.0
    row["planned_notional"] = 0.0
    row["blocked_by"] = list(row.get("blocked_by", [])) + [blocked_by_reason]
    return _sync_layer4_authorized_aliases(row)


def _apply_cycle_trade_limits(plan: list[dict]) -> list[dict]:
    buys_used = 0
    sells_used = 0
    trades_used = 0
    buy_notional_used = 0.0

    limited_plan = []

    for row in plan:
        decision = row.get("decision")

        if decision not in {"BUY", "SELL"}:
            limited_plan.append(row)
            continue

        planned_notional = safe_float(row.get("planned_notional"), 0.0)

        if planned_notional <= 0:
            limited_plan.append(row)
            continue

        if trades_used >= L3_MAX_TRADES_PER_CYCLE:
            limited_plan.append(
                _defer_trade(
                    row,
                    reason="cycle_trade_limit_deferred",
                    blocked_by_reason="cycle_trade_limit",
                )
            )
            continue

        if decision == "BUY":
            if buys_used >= L3_MAX_BUYS_PER_CYCLE:
                limited_plan.append(
                    _defer_trade(
                        row,
                        reason="cycle_buy_limit_deferred",
                        blocked_by_reason="cycle_buy_limit",
                    )
                )
                continue

            if buy_notional_used + planned_notional > L3_MAX_TOTAL_BUY_NOTIONAL_PER_CYCLE:
                limited_plan.append(
                    _defer_trade(
                        row,
                        reason="cycle_buy_notional_limit_deferred",
                        blocked_by_reason="cycle_buy_notional_limit",
                    )
                )
                continue

            buys_used += 1
            trades_used += 1
            buy_notional_used += planned_notional
            limited_plan.append(row)
            continue

        if decision == "SELL":
            if sells_used >= L3_MAX_SELLS_PER_CYCLE:
                limited_plan.append(
                    _defer_trade(
                        row,
                        reason="cycle_sell_limit_deferred",
                        blocked_by_reason="cycle_sell_limit",
                    )
                )
                continue

            sells_used += 1
            trades_used += 1
            limited_plan.append(row)
            continue

    return limited_plan


def _cap_planned_notional(decision: str, delta_value: float, current_qty: float, equity: float) -> tuple[float, str | None]:
    """
    Cap how aggressively Layer 3 moves toward a target in one cycle.

    Returns:
        capped_notional, cap_reason
    """
    abs_delta = abs(delta_value)

    if decision == "BUY":
        if current_qty <= 0:
            max_by_weight = equity * L3_MAX_NEW_POSITION_WEIGHT_PER_CYCLE
            cap = min(abs_delta, L3_MAX_BUY_NOTIONAL_PER_CYCLE, max_by_weight)
            reason = "new_position_scale_in_capped" if cap < abs_delta else None
            return cap, reason

        max_by_weight = equity * L3_MAX_ADD_WEIGHT_PER_CYCLE
        cap = min(abs_delta, L3_MAX_BUY_NOTIONAL_PER_CYCLE, max_by_weight)
        reason = "add_position_scale_in_capped" if cap < abs_delta else None
        return cap, reason

    if decision == "SELL":
        max_by_weight = equity * L3_MAX_TRIM_WEIGHT_PER_CYCLE
        cap = min(abs_delta, L3_MAX_SELL_NOTIONAL_PER_CYCLE, max_by_weight)
        reason = "sell_scale_out_capped" if cap < abs_delta else None
        return cap, reason

    return abs_delta, None


def _maybe_bootstrap_layer3_confirmation(
    rebalance: dict,
    target_symbols: set[str],
    required_seen_count: int,
    market_is_open: bool,
) -> list[str]:
    """
    Warm-start target confirmation after startup/redeploy.

    Bootstrap is allowed only when:
    - LAYER3_BOOTSTRAP_CONFIRMATION_ENABLED=true
    - the symbol is currently in the target portfolio
    - the symbol has enough recent bars
    - either the market is open OR LAYER3_MARKET_HOURS_ONLY=false
    """

    bootstrap_enabled = _layer3_bool_setting(
        "layer3_bootstrap_confirmation_enabled",
        True,
    )

    if not bootstrap_enabled:
        return []

    if rebalance.get("bootstrap_confirmation_applied"):
        return []

    market_hours_only = _layer3_bool_setting(
        "layer3_market_hours_only",
        True,
    )

    if market_hours_only and not market_is_open:
        logging.info(
            "[Layer3Bootstrap] Skipping bootstrap because market is closed "
            "and LAYER3_MARKET_HOURS_ONLY=true."
        )
        return []

    min_bar_count = _layer3_int_setting(
        "layer3_bootstrap_min_bar_count",
        8,
    )

    latest = app_state.get("layers", {}).get("latest", {})
    bar_counts = latest.get("bar_counts", {}) or {}

    target_seen_counts = rebalance.setdefault("target_seen_counts", {})

    bootstrapped_symbols = []

    for symbol in sorted(target_symbols):
        bars_available = safe_int(bar_counts.get(symbol, 0), 0)

        if bars_available < min_bar_count:
            logging.info(
                "[Layer3Bootstrap] %s not bootstrapped; bars_available=%s "
                "min_required=%s",
                symbol,
                bars_available,
                min_bar_count,
            )
            continue

        current_seen = safe_int(target_seen_counts.get(symbol, 0), 0)
        target_seen_counts[symbol] = max(
            current_seen,
            required_seen_count,
        )
        bootstrapped_symbols.append(symbol)

    rebalance["bootstrap_confirmation_applied"] = True
    rebalance["bootstrap_confirmation_symbols"] = bootstrapped_symbols

    if bootstrapped_symbols:
        logging.warning(
            "[Layer3Bootstrap] Bootstrapped target confirmation for symbols=%s "
            "required_seen_count=%s min_bar_count=%s",
            bootstrapped_symbols,
            required_seen_count,
            min_bar_count,
        )
    else:
        logging.info(
            "[Layer3Bootstrap] Bootstrap completed but no symbols qualified."
        )

    return bootstrapped_symbols


def run_layer3_dry_run() -> dict:
    """
    Broker-aware Layer 3 dry-run planner.

    This does not place orders.
    It compares Layer 2 target weights against real Alpaca account/position state
    and produces a BUY / SELL / HOLD / SKIP plan.
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
        return {"plan": [], "summary": summary}

    target = latest.get("target_portfolio", {})
    target_weights, target_cash_pct, target_meta = clean_target_portfolio(target)

    cycle_id = int(rebalance.get("last_cycle_id", 0) or 0) + 1
    plan_created_dt = datetime.now(timezone.utc)
    plan_id, plan_created_at, plan_expires_at, plan_ttl_seconds = _build_plan_ids(
        cycle_id,
        plan_created_dt,
    )
    timestamp = plan_created_at

    account = _get_account_snapshot()
    equity = safe_float(account.get("equity"), 0.0)
    cash = safe_float(account.get("cash"), 0.0)

    if equity <= 0:
        summary = {
            "status": "error",
            "dry_run": True,
            "cycle_id": cycle_id,
            "plan_id": plan_id,
            "timestamp": timestamp,
            "plan_expires_at": plan_expires_at,
            "reason": "missing_or_invalid_equity",
            "equity": equity,
        }
        rebalance["last_cycle_id"] = cycle_id
        rebalance["last_run_at"] = timestamp
        rebalance["last_plan"] = []
        rebalance["last_summary"] = summary
        rebalance["last_error"] = "missing_or_invalid_equity"

        logging.warning("[Layer3] Cannot build rebalance plan because equity is invalid: %s", equity)
        return {"plan": [], "summary": summary}

    ranked_prices = _ranked_price_map(latest)
    positions = _get_positions_snapshot()
    open_order_symbols, open_order_details = _get_open_order_symbols()

    seen_counts, absent_counts = _update_target_stability(
        rebalance,
        target_weights,
        positions,
    )

    market_is_open = bool(rebalance.get("market_is_open", False))
    confirmation_updates_allowed = bool(
        rebalance.get("confirmation_updates_allowed", False)
    )

    target_symbols = set(target_weights.keys())
    bootstrapped_symbols = []

    if confirmation_updates_allowed:
        bootstrapped_symbols = _maybe_bootstrap_layer3_confirmation(
            rebalance=rebalance,
            target_symbols=target_symbols,
            required_seen_count=L3_REQUIRE_TARGET_CONFIRMATION_CYCLES,
            market_is_open=market_is_open,
        )

    fail_safe_active = bool(app_state.get("fail_safes", {}).get("state"))

    plan = []
    symbol_universe = sorted(set(target_weights.keys()) | set(positions.keys()))

    for symbol in symbol_universe:
        position = positions.get(symbol, {})
        current_qty = safe_float(position.get("qty"), 0.0)

        live_price, price_source = _get_price_for_symbol(symbol, ranked_prices, position)

        target_weight = safe_float(target_weights.get(symbol), 0.0)

        blocked_by = []
        open_order_exists = symbol in open_order_symbols

        if open_order_exists:
            blocked_by.append("open_order_exists")

        if fail_safe_active:
            blocked_by.append("fail_safe_active")

        if live_price <= 0:
            current_value = safe_float(position.get("market_value"), 0.0)
            current_weight = current_value / equity if equity > 0 else 0.0
            target_value = target_weight * equity
            delta_value = target_value - current_value
            delta_weight = target_weight - current_weight

            target_qty = 0.0
            qty_delta = 0.0

            row = _build_row(
                plan_id=plan_id,
                row_id=f"{plan_id}:{symbol}",
                plan_created_at=plan_created_at,
                plan_expires_at=plan_expires_at,
                plan_ttl_seconds=plan_ttl_seconds,
                cycle_id=cycle_id,
                timestamp=timestamp,
                symbol=symbol,
                decision="SKIP",
                reason="missing_price",
                target_weight=target_weight,
                current_weight=current_weight,
                target_value=target_value,
                current_value=current_value,
                delta_value=delta_value,
                delta_weight=delta_weight,
                relative_drift=0.0,
                live_price=0.0,
                price_source=price_source,
                current_qty=current_qty,
                target_qty=target_qty,
                qty_delta=qty_delta,
                planned_qty=0.0,
                planned_notional=0.0,
                target_seen_count=seen_counts.get(symbol, 0),
                target_absent_count=absent_counts.get(symbol, 0),
                open_order_exists=open_order_exists,
                open_order_detail=open_order_details.get(symbol),
                blocked_by=blocked_by,
                account=account,
                target_meta=target_meta,
            )
            plan.append(row)
            continue

        current_value = current_qty * live_price
        current_weight = current_value / equity if equity > 0 else 0.0

        target_value = target_weight * equity
        delta_value = target_value - current_value
        delta_weight = target_weight - current_weight

        target_qty = _target_qty_from_value(target_value, live_price)
        qty_delta = target_qty - current_qty

        relative_drift = abs(delta_value) / max(abs(target_value), abs(current_value), 1.0)

        target_seen_count = int(seen_counts.get(symbol, 0) or 0)
        target_absent_count = int(absent_counts.get(symbol, 0) or 0)

        decision = "HOLD"
        reason = "already_aligned"
        planned_qty = 0.0
        planned_notional = 0.0

        drift_too_small = (
            abs(delta_value) < L3_MIN_TRADE_VALUE_DOLLARS
            or (
                abs(delta_weight) < L3_MIN_ABS_WEIGHT_DRIFT
                and relative_drift < L3_MIN_RELATIVE_DRIFT
            )
        )

        if open_order_exists:
            decision = "SKIP"
            reason = "open_order_exists"

        elif target_weight <= 0 and current_qty > 0:
            # Symbol is held but no longer in Layer 2 target.
            if target_absent_count < L3_REQUIRE_EXIT_CONFIRMATION_CYCLES:
                decision = "HOLD"
                reason = "exit_not_confirmed"
            else:
                decision = "SELL"
                reason = "target_removed_confirmed"
                planned_qty = current_qty if not L3_WHOLE_SHARES_ONLY else float(int(current_qty))
                planned_notional = planned_qty * live_price

                if planned_qty <= 0:
                    decision = "HOLD"
                    reason = "planned_sell_qty_zero"

        elif delta_value > 0:
            # Underweight or new target position.
            if fail_safe_active:
                decision = "SKIP"
                reason = "fail_safe_active_blocks_buy"

            elif target_seen_count < L3_REQUIRE_TARGET_CONFIRMATION_CYCLES:
                decision = "HOLD"
                reason = "target_not_confirmed"

            elif drift_too_small:
                decision = "HOLD"
                reason = "buy_drift_below_threshold"

            else:
                decision = "BUY"
                reason = "underweight_vs_target"

                capped_notional, cap_reason = _cap_planned_notional(
                    decision="BUY",
                    delta_value=delta_value,
                    current_qty=current_qty,
                    equity=equity,
                )

                planned_qty = _whole_share_qty(capped_notional, live_price)
                planned_notional = planned_qty * live_price

                if cap_reason:
                    reason = cap_reason

                if planned_qty <= 0:
                    decision = "HOLD"
                    reason = "planned_buy_qty_zero"

        elif delta_value < 0 and current_qty > 0:
            # Overweight existing position.
            if drift_too_small:
                decision = "HOLD"
                reason = "sell_drift_below_threshold"
            else:
                decision = "SELL"
                reason = "overweight_vs_target"

                capped_notional, cap_reason = _cap_planned_notional(
                    decision="SELL",
                    delta_value=delta_value,
                    current_qty=current_qty,
                    equity=equity,
                )

                planned_qty = min(current_qty, _whole_share_qty(capped_notional, live_price))
                planned_notional = planned_qty * live_price

                if cap_reason:
                    reason = cap_reason

                if planned_qty <= 0:
                    decision = "HOLD"
                    reason = "planned_sell_qty_zero"

        row = _build_row(
            plan_id=plan_id,
            row_id=f"{plan_id}:{symbol}",
            plan_created_at=plan_created_at,
            plan_expires_at=plan_expires_at,
            plan_ttl_seconds=plan_ttl_seconds,
            cycle_id=cycle_id,
            timestamp=timestamp,
            symbol=symbol,
            decision=decision,
            reason=reason,
            target_weight=target_weight,
            current_weight=current_weight,
            target_value=target_value,
            current_value=current_value,
            delta_value=delta_value,
            delta_weight=delta_weight,
            relative_drift=relative_drift,
            live_price=live_price,
            price_source=price_source,
            current_qty=current_qty,
            target_qty=target_qty,
            qty_delta=qty_delta,
            planned_qty=planned_qty,
            planned_notional=planned_notional,
            target_seen_count=target_seen_count,
            target_absent_count=target_absent_count,
            open_order_exists=open_order_exists,
            open_order_detail=open_order_details.get(symbol),
            blocked_by=blocked_by,
            account=account,
            target_meta=target_meta,
        )

        plan.append(row)

    # Sells first, then buys, then holds/skips.
    plan.sort(key=_plan_priority)
    plan = _apply_cycle_trade_limits(plan)

    # Cash estimate pass.
    estimated_cash = cash

    for row in plan:
        row["cash_before_estimate"] = round(estimated_cash, 2)

        if row["decision"] == "SELL":
            estimated_cash += safe_float(row.get("planned_notional"), 0.0)

        elif row["decision"] == "BUY":
            planned_notional = safe_float(row.get("planned_notional"), 0.0)
            live_price = safe_float(row.get("live_price"), 0.0)

            if planned_notional > estimated_cash:
                adjusted_qty = _whole_share_qty(estimated_cash, live_price)
                adjusted_notional = adjusted_qty * live_price

                if adjusted_qty <= 0:
                    row["decision"] = "SKIP"
                    row["reason"] = "insufficient_cash"
                    row["planned_qty"] = 0.0
                    row["planned_notional"] = 0.0
                    row["blocked_by"] = list(row.get("blocked_by", [])) + ["insufficient_cash"]
                    _sync_layer4_authorized_aliases(row)
                else:
                    row["reason"] = "underweight_vs_target_cash_adjusted"
                    row["planned_qty"] = round(adjusted_qty, 6)
                    row["planned_notional"] = round(adjusted_notional, 2)
                    _sync_layer4_authorized_aliases(row)
                    estimated_cash -= adjusted_notional
            else:
                estimated_cash -= planned_notional

        row["cash_after_estimate"] = round(estimated_cash, 2)

    decision_counts = {}
    for row in plan:
        decision = row.get("decision", "UNKNOWN")
        decision_counts[decision] = decision_counts.get(decision, 0) + 1

    summary = {
        "status": "ok",
        "dry_run": True,
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "timestamp": timestamp,
        "plan_created_at": plan_created_at,
        "plan_expires_at": plan_expires_at,
        "plan_ttl_seconds": plan_ttl_seconds,
        "execution_layer": "layer4",
        "execution_mode": "layer4_direct_compat",

        "market_is_open": market_is_open,
        "confirmation_updates_allowed": confirmation_updates_allowed,
        "confirmation_updates_blocked_reason": rebalance.get(
            "confirmation_updates_blocked_reason"
        ),
        "bootstrap_confirmation_applied": rebalance.get(
            "bootstrap_confirmation_applied",
            False,
        ),
        "bootstrap_confirmation_symbols": rebalance.get(
            "bootstrap_confirmation_symbols",
            [],
        ),

        "target_symbol_count": len(target_weights),
        "target_cash_pct": round(target_cash_pct, 6),
        "target_total_weight": round(sum(target_weights.values()), 6),
        "market_strength": target_meta.get("market_strength"),

        "account_source": account.get("source"),
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "estimated_cash_after_plan": round(estimated_cash, 2),

        "positions_count": len(positions),
        "open_order_count": len(open_order_symbols),

        "plan_count": len(plan),
        "decision_counts": decision_counts,
        "fail_safe_active": fail_safe_active,

        "cycle_trade_limits": {
            "max_trades": L3_MAX_TRADES_PER_CYCLE,
            "max_buys": L3_MAX_BUYS_PER_CYCLE,
            "max_sells": L3_MAX_SELLS_PER_CYCLE,
            "max_total_buy_notional": L3_MAX_TOTAL_BUY_NOTIONAL_PER_CYCLE,
        },
    }

    _mark_previous_active_execution_plan_replaced(
        layers,
        new_plan_id=plan_id,
        cycle_id=cycle_id,
        timestamp=timestamp,
    )

    active_execution_plan = _store_active_execution_plan(
        layers,
        plan_id=plan_id,
        created_at=plan_created_at,
        expires_at=plan_expires_at,
        ttl_seconds=plan_ttl_seconds,
        summary=summary,
        plan=plan,
    )

    rebalance["last_cycle_id"] = cycle_id
    rebalance["last_run_at"] = timestamp
    rebalance["last_plan"] = plan
    rebalance["last_summary"] = summary
    rebalance["last_error"] = None
    rebalance["active_plan_id"] = plan_id

    logging.info(
        "[Layer3] Dry-run drift plan complete | cycle_id=%s decisions=%s equity=$%.2f cash=$%.2f",
        cycle_id,
        decision_counts,
        equity,
        cash,
    )

    for row in plan:
        logging.info(
            "[Layer3Plan] cycle=%s plan_id=%s %s decision=%s reason=%s "
            "current=%.2f%% target=%.2f%% current_qty=%s target_qty=%s "
            "qty_delta=%s delta=$%.2f auth_qty=%s auth_notional=$%.2f price=$%.2f",
            row["cycle_id"],
            row.get("plan_id"),
            row["symbol"],
            row["decision"],
            row["reason"],
            row["current_weight"] * 100,
            row["target_weight"] * 100,
            row.get("current_qty"),
            row.get("target_qty"),
            row.get("qty_delta"),
            row["delta_value"],
            row["planned_qty"],
            row["planned_notional"],
            row["live_price"],
        )

    return {
        "plan": plan,
        "summary": summary,
        "active_execution_plan": active_execution_plan,
    }