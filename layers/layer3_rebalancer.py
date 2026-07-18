import logging
from datetime import datetime, timezone, timedelta

from core.market_clock import get_market_is_open
from utils.numeric import safe_float, safe_int, safe_round
from utils.symbols import normalize_symbol

from core.state import app_state
from config import runtime_config as config

from layers.layer_csv import (
    append_layer3_plan_rows,
    append_layer_portfolio_snapshot_rows,
)

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


def _layer3_float_setting(name: str, default: float) -> float:
    return safe_float(
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


def _get_off_hours_warmup_bootstrap_symbols() -> tuple[set[str], dict]:
    """
    Return warmup target symbols that are fresh enough to trust for bootstrap.

    Closed-market warmup is useful for target continuity, but a symbol should
    not receive instant Layer 3 confirmation at the open just because it was in
    a warmup target built from stale bars. If freshness diagnostics are missing,
    fall back to the previous behavior and mark that in diagnostics.
    """
    warmup = app_state.get("layers", {}).get("last_off_hours_warmup")

    diagnostics = {
        "warmup_present": isinstance(warmup, dict),
        "warmup_target_symbols": [],
        "warmup_freshness_available": False,
        "eligible_symbols": [],
        "stale_symbols": [],
        "missing_age_symbols": [],
        "max_age_minutes": None,
    }

    if not isinstance(warmup, dict):
        return set(), diagnostics

    target = warmup.get("target_portfolio", {})
    target_weights, _, _ = clean_target_portfolio(target)
    target_symbols = set(target_weights.keys())
    diagnostics["warmup_target_symbols"] = sorted(target_symbols)

    if not target_symbols:
        return set(), diagnostics

    freshness_report = warmup.get("freshness_report", {})
    if not isinstance(freshness_report, dict):
        freshness_report = {}

    max_age_minutes = _layer3_float_setting(
        "layer3_warmup_bootstrap_max_age_minutes",
        safe_float(freshness_report.get("max_age_minutes"), 35.0),
    )
    diagnostics["max_age_minutes"] = max_age_minutes

    latest_ages = freshness_report.get("latest_bar_ages_minutes", {})
    fresh_symbols = set(
        _norm_symbol(s)
        for s in freshness_report.get("fresh_symbols", []) or []
    )

    if not isinstance(latest_ages, dict) or not latest_ages:
        # Backward-compatible fallback for older warmup snapshots that did not
        # store age diagnostics. New monitor code fills this in.
        diagnostics["warmup_freshness_available"] = False
        diagnostics["eligible_symbols"] = sorted(target_symbols)
        return target_symbols, diagnostics

    diagnostics["warmup_freshness_available"] = True

    eligible_symbols = set()
    stale_symbols = []
    missing_age_symbols = []

    for symbol in sorted(target_symbols):
        raw_age = latest_ages.get(symbol)

        if raw_age is None:
            # If the report explicitly says the symbol is fresh, allow it even
            # when age is missing. Otherwise avoid bootstrapping it.
            if symbol in fresh_symbols:
                eligible_symbols.add(symbol)
            else:
                missing_age_symbols.append(symbol)
            continue

        age = safe_float(raw_age, -1.0)
        if age >= 0.0 and age <= max_age_minutes:
            eligible_symbols.add(symbol)
        else:
            stale_symbols.append(symbol)

    diagnostics["eligible_symbols"] = sorted(eligible_symbols)
    diagnostics["stale_symbols"] = stale_symbols
    diagnostics["missing_age_symbols"] = missing_age_symbols

    return eligible_symbols, diagnostics


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

    Broker account data is the only source trusted for executable Layer 3
    planning. Fallback values are returned only for diagnostics so the caller
    can explicitly block trading instead of sizing a portfolio from stale
    local state.
    """
    client = app_state.get("trading_client")
    broker_error = None

    if client:
        try:
            account = client.get_account()

            return {
                "source": "alpaca_account",
                "broker_snapshot_ok": True,
                "account_snapshot_error": None,
                "equity": safe_float(getattr(account, "equity", 0.0), 0.0),
                "cash": safe_float(getattr(account, "cash", 0.0), 0.0),
                "buying_power": safe_float(getattr(account, "buying_power", 0.0), 0.0),
            }

        except Exception as exc:
            broker_error = str(exc)
            logging.warning("[Layer3] Failed to fetch Alpaca account snapshot.", exc_info=True)
    else:
        broker_error = "missing_trading_client"

    try:
        tracker = app_state.get("services", {}).get("balance_tracker", {}).get("instance")
        if tracker and hasattr(tracker, "get_balance"):
            balance = tracker.get_balance()
            return {
                "source": "balance_tracker",
                "broker_snapshot_ok": False,
                "account_snapshot_error": broker_error,
                "equity": safe_float(balance.get("equity"), 0.0),
                "cash": safe_float(balance.get("balance"), 0.0),
                "buying_power": safe_float(balance.get("balance"), 0.0),
            }
    except Exception as exc:
        logging.warning("[Layer3] Failed to use balance tracker fallback.", exc_info=True)
        if broker_error is None:
            broker_error = str(exc)

    balance_state = app_state.get("services", {}).get("balance_tracker", {})
    return {
        "source": "app_state_balance_tracker",
        "broker_snapshot_ok": False,
        "account_snapshot_error": broker_error,
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


def update_layer3_target_stability_state(
    planner_state: dict,
    target_weights: dict,
    positions: dict,
    *,
    market_is_open: bool,
    market_hours_only: bool,
    log_prefix: str = "Layer3",
) -> tuple[dict, dict]:
    """
    Shared target-membership confirmation state for REST and LIVE planners.

    This intentionally preserves the current cycle-based confirmation behavior.
    The later five-minute cadence patch will add distinct-bar evidence so a
    duplicate input bar cannot advance these counters.
    """
    seen_counts = planner_state.setdefault("target_seen_counts", {})
    absent_counts = planner_state.setdefault("target_absent_counts", {})

    target_symbols = set(target_weights.keys())
    all_symbols = set(target_symbols) | set(positions.keys())

    confirmation_updates_allowed = bool(
        market_is_open or not market_hours_only
    )

    planner_state["market_is_open"] = bool(market_is_open)
    planner_state["confirmation_updates_allowed"] = confirmation_updates_allowed
    planner_state["confirmation_updates_blocked_reason"] = (
        None if confirmation_updates_allowed else "market_closed"
    )

    if not confirmation_updates_allowed:
        logging.info(
            "[%s] Confirmation counters frozen because market is closed "
            "and market-hours-only confirmation is enabled.",
            log_prefix,
        )
        return seen_counts, absent_counts

    for symbol in all_symbols:
        if symbol in target_symbols:
            seen_counts[symbol] = safe_int(
                seen_counts.get(symbol, 0),
                0,
            ) + 1
            absent_counts[symbol] = 0
        else:
            absent_counts[symbol] = safe_int(
                absent_counts.get(symbol, 0),
                0,
            ) + 1
            seen_counts[symbol] = 0

    planner_state["last_confirmation_update_at"] = datetime.now(
        timezone.utc
    ).isoformat()

    keep_symbols = all_symbols

    for symbol in list(seen_counts.keys()):
        if (
            symbol not in keep_symbols
            and safe_int(seen_counts.get(symbol, 0), 0) <= 0
        ):
            seen_counts.pop(symbol, None)

    for symbol in list(absent_counts.keys()):
        if (
            symbol not in keep_symbols
            and safe_int(absent_counts.get(symbol, 0), 0) <= 0
        ):
            absent_counts.pop(symbol, None)

    return seen_counts, absent_counts


def _update_target_stability(
    rebalance: dict,
    target_weights: dict,
    positions: dict,
) -> tuple[dict, dict]:
    """
    Production REST wrapper around the shared confirmation-state function.
    """
    market_is_open = get_market_is_open(app_state)
    market_hours_only = _layer3_bool_setting(
        "layer3_market_hours_only",
        True,
    )

    return update_layer3_target_stability_state(
        rebalance,
        target_weights,
        positions,
        market_is_open=market_is_open,
        market_hours_only=market_hours_only,
        log_prefix="Layer3",
    )


def get_layer3_price_for_symbol(
    symbol: str,
    ranked_prices: dict,
    position: dict | None,
    *,
    last_trade_prices: dict | None = None,
) -> tuple[float, str]:
    """
    Shared price selection for production REST and shadow LIVE planning.

    Priority:
    1. Source-specific ranked/input-bar price
    2. Optional source-specific latest-trade price map
    3. Position current price
    4. Position average entry price
    """
    symbol = _norm_symbol(symbol)

    price = safe_float(ranked_prices.get(symbol), 0.0)
    if price > 0:
        return price, "ranked_last_price"

    last_trade_prices = last_trade_prices or {}
    price = safe_float(last_trade_prices.get(symbol), 0.0)
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


def _get_price_for_symbol(
    symbol: str,
    ranked_prices: dict,
    position: dict | None,
) -> tuple[float, str]:
    """Production wrapper preserving the existing REST price fallbacks."""
    return get_layer3_price_for_symbol(
        symbol,
        ranked_prices,
        position,
        last_trade_prices=app_state.get(
            "last_trade_price_by_symbol",
            {},
        ),
    )


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
    planner_source,
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
        "planner_source": str(
            planner_source or "REST"
        ).upper().strip(),
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


def build_layer3_plan_from_snapshots(
    *,
    planner_source: str,
    target: dict,
    account: dict,
    positions: dict,
    ranked_prices: dict,
    seen_counts: dict,
    absent_counts: dict,
    cycle_id: int,
    plan_id: str,
    plan_created_at: str,
    plan_expires_at: str,
    plan_ttl_seconds: int,
    open_order_symbols: set | None = None,
    open_order_details: dict | None = None,
    fail_safe_active: bool = False,
    opening_transition: dict | None = None,
    last_trade_prices: dict | None = None,
) -> dict:
    """
    Shared Layer 3 planning kernel used by REST production and LIVE shadow.

    All source-specific market data, portfolio state, and confirmation state are
    supplied by the caller. This function does not read broker state, mutate the
    production Layer 3 handoff, append CSVs, or submit orders.
    """
    planner_source = str(
        planner_source or "REST"
    ).upper().strip()

    target_weights, target_cash_pct, target_meta = (
        clean_target_portfolio(target)
    )

    equity = safe_float(account.get("equity"), 0.0)
    cash = safe_float(account.get("cash"), 0.0)
    timestamp = plan_created_at

    open_order_symbols = set(open_order_symbols or set())
    open_order_details = open_order_details or {}
    opening_transition = opening_transition or {}
    last_trade_prices = last_trade_prices or {}

    plan = []
    symbol_universe = sorted(
        set(target_weights.keys())
        | set(positions.keys())
    )

    for symbol in symbol_universe:
        position = positions.get(symbol, {})
        current_qty = safe_float(
            position.get("qty"),
            0.0,
        )

        live_price, price_source = (
            get_layer3_price_for_symbol(
                symbol,
                ranked_prices,
                position,
                last_trade_prices=last_trade_prices,
            )
        )

        target_weight = safe_float(
            target_weights.get(symbol),
            0.0,
        )

        blocked_by = []
        open_order_exists = symbol in open_order_symbols

        if open_order_exists:
            blocked_by.append("open_order_exists")

        if fail_safe_active:
            blocked_by.append("fail_safe_active")

        if live_price <= 0:
            current_value = safe_float(
                position.get("market_value"),
                0.0,
            )
            current_weight = (
                current_value / equity
                if equity > 0
                else 0.0
            )
            target_value = target_weight * equity
            delta_value = target_value - current_value
            delta_weight = target_weight - current_weight

            row = _build_row(
                planner_source=planner_source,
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
                target_qty=0.0,
                qty_delta=0.0,
                planned_qty=0.0,
                planned_notional=0.0,
                target_seen_count=seen_counts.get(
                    symbol,
                    0,
                ),
                target_absent_count=absent_counts.get(
                    symbol,
                    0,
                ),
                open_order_exists=open_order_exists,
                open_order_detail=open_order_details.get(
                    symbol
                ),
                blocked_by=blocked_by,
                account=account,
                target_meta=target_meta,
            )
            plan.append(row)
            continue

        current_value = current_qty * live_price
        current_weight = (
            current_value / equity
            if equity > 0
            else 0.0
        )

        target_value = target_weight * equity
        delta_value = target_value - current_value
        delta_weight = target_weight - current_weight

        target_qty = _target_qty_from_value(
            target_value,
            live_price,
        )
        qty_delta = target_qty - current_qty

        relative_drift = abs(delta_value) / max(
            abs(target_value),
            abs(current_value),
            1.0,
        )

        target_seen_count = int(
            seen_counts.get(symbol, 0) or 0
        )
        target_absent_count = int(
            absent_counts.get(symbol, 0) or 0
        )

        decision = "HOLD"
        reason = "already_aligned"
        planned_qty = 0.0
        planned_notional = 0.0

        drift_too_small = (
            abs(delta_value)
            < L3_MIN_TRADE_VALUE_DOLLARS
            or (
                abs(delta_weight)
                < L3_MIN_ABS_WEIGHT_DRIFT
                and relative_drift
                < L3_MIN_RELATIVE_DRIFT
            )
        )

        if open_order_exists:
            decision = "SKIP"
            reason = "open_order_exists"

        elif target_weight <= 0 and current_qty > 0:
            if (
                target_absent_count
                < L3_REQUIRE_EXIT_CONFIRMATION_CYCLES
            ):
                decision = "HOLD"
                reason = "exit_not_confirmed"
            else:
                decision = "SELL"

                if opening_transition.get("active"):
                    capped_notional, _ = (
                        _cap_planned_notional(
                            decision="SELL",
                            delta_value=delta_value,
                            current_qty=current_qty,
                            equity=equity,
                        )
                    )

                    planned_qty = min(
                        current_qty,
                        _whole_share_qty(
                            capped_notional,
                            live_price,
                        ),
                    )
                    planned_notional = (
                        planned_qty * live_price
                    )
                    reason = (
                        "opening_target_removed_"
                        "scale_out_capped"
                    )
                else:
                    reason = "target_removed_confirmed"
                    planned_qty = (
                        current_qty
                        if not L3_WHOLE_SHARES_ONLY
                        else float(int(current_qty))
                    )
                    planned_notional = (
                        planned_qty * live_price
                    )

                if planned_qty <= 0:
                    decision = "HOLD"
                    reason = "planned_sell_qty_zero"

        elif delta_value > 0:
            if fail_safe_active:
                decision = "SKIP"
                reason = "fail_safe_active_blocks_buy"

            elif (
                target_seen_count
                < L3_REQUIRE_TARGET_CONFIRMATION_CYCLES
            ):
                decision = "HOLD"
                reason = "target_not_confirmed"

            elif drift_too_small:
                decision = "HOLD"
                reason = "buy_drift_below_threshold"

            else:
                decision = "BUY"
                reason = "underweight_vs_target"

                capped_notional, cap_reason = (
                    _cap_planned_notional(
                        decision="BUY",
                        delta_value=delta_value,
                        current_qty=current_qty,
                        equity=equity,
                    )
                )

                planned_qty = _whole_share_qty(
                    capped_notional,
                    live_price,
                )
                planned_notional = (
                    planned_qty * live_price
                )

                if cap_reason:
                    reason = cap_reason

                if planned_qty <= 0:
                    decision = "HOLD"
                    reason = "planned_buy_qty_zero"

        elif delta_value < 0 and current_qty > 0:
            if drift_too_small:
                decision = "HOLD"
                reason = "sell_drift_below_threshold"
            else:
                decision = "SELL"
                reason = "overweight_vs_target"

                capped_notional, cap_reason = (
                    _cap_planned_notional(
                        decision="SELL",
                        delta_value=delta_value,
                        current_qty=current_qty,
                        equity=equity,
                    )
                )

                planned_qty = min(
                    current_qty,
                    _whole_share_qty(
                        capped_notional,
                        live_price,
                    ),
                )
                planned_notional = (
                    planned_qty * live_price
                )

                if cap_reason:
                    reason = cap_reason

                if planned_qty <= 0:
                    decision = "HOLD"
                    reason = "planned_sell_qty_zero"

        row = _build_row(
            planner_source=planner_source,
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
            open_order_detail=open_order_details.get(
                symbol
            ),
            blocked_by=blocked_by,
            account=account,
            target_meta=target_meta,
        )

        plan.append(row)

    plan.sort(key=_plan_priority)
    plan = _apply_cycle_trade_limits(plan)

    estimated_cash = cash

    for row in plan:
        row["cash_before_estimate"] = round(
            estimated_cash,
            2,
        )

        if row["decision"] == "SELL":
            estimated_cash += safe_float(
                row.get("planned_notional"),
                0.0,
            )

        elif row["decision"] == "BUY":
            planned_notional = safe_float(
                row.get("planned_notional"),
                0.0,
            )
            live_price = safe_float(
                row.get("live_price"),
                0.0,
            )

            if planned_notional > estimated_cash:
                adjusted_qty = _whole_share_qty(
                    estimated_cash,
                    live_price,
                )
                adjusted_notional = (
                    adjusted_qty * live_price
                )

                if adjusted_qty <= 0:
                    row["decision"] = "SKIP"
                    row["reason"] = "insufficient_cash"
                    row["planned_qty"] = 0.0
                    row["planned_notional"] = 0.0
                    row["blocked_by"] = list(
                        row.get("blocked_by", [])
                    ) + ["insufficient_cash"]
                    _sync_layer4_authorized_aliases(
                        row
                    )
                else:
                    row["reason"] = (
                        "underweight_vs_target_"
                        "cash_adjusted"
                    )
                    row["planned_qty"] = round(
                        adjusted_qty,
                        6,
                    )
                    row["planned_notional"] = round(
                        adjusted_notional,
                        2,
                    )
                    _sync_layer4_authorized_aliases(
                        row
                    )
                    estimated_cash -= (
                        adjusted_notional
                    )
            else:
                estimated_cash -= planned_notional

        row["cash_after_estimate"] = round(
            estimated_cash,
            2,
        )

    decision_counts = {}

    for row in plan:
        row_decision = row.get(
            "decision",
            "UNKNOWN",
        )
        decision_counts[row_decision] = (
            decision_counts.get(
                row_decision,
                0,
            ) + 1
        )

    return {
        "planner_source": planner_source,
        "plan": plan,
        "decision_counts": decision_counts,
        "estimated_cash_after_plan": round(
            estimated_cash,
            2,
        ),
        "target_weights": target_weights,
        "target_cash_pct": target_cash_pct,
        "target_meta": target_meta,
    }


def build_layer3_shadow_plan(
    *,
    planner_source: str,
    target: dict,
    account: dict,
    positions: dict,
    ranked_prices: dict,
    planner_state: dict,
    market_is_open: bool,
    cycle_id: int,
    bar_counts: dict | None = None,
    bootstrap_eligible_symbols: set[str] | None = None,
    open_order_symbols: set | None = None,
    open_order_details: dict | None = None,
    fail_safe_active: bool = False,
    last_trade_prices: dict | None = None,
) -> dict:
    """
    Build an isolated shadow Layer 3 plan with the production planning kernel.

    The caller owns planner_state, allowing REST and LIVE shadow portfolios to
    maintain independent confirmation history without touching production state.
    """
    planner_source = str(
        planner_source or "SHADOW"
    ).upper().strip()

    target_weights, _, _ = clean_target_portfolio(
        target
    )

    market_hours_only = _layer3_bool_setting(
        "layer3_market_hours_only",
        True,
    )

    open_session_info = (
        _prepare_market_open_session_state(
            planner_state,
            market_is_open=market_is_open,
        )
    )
    opening_transition = _opening_transition_info(
        planner_state,
        market_is_open,
    )

    seen_counts, absent_counts = (
        update_layer3_target_stability_state(
            planner_state,
            target_weights,
            positions,
            market_is_open=market_is_open,
            market_hours_only=market_hours_only,
            log_prefix=(
                f"Layer3Shadow:{planner_source}"
            ),
        )
    )

    bootstrap_enabled = _layer3_bool_setting(
        "layer3_bootstrap_confirmation_enabled",
        True,
    )
    min_bar_count = _layer3_int_setting(
        "layer3_bootstrap_min_bar_count",
        8,
    )
    bar_counts = bar_counts or {}

    if (
        bootstrap_enabled
        and market_is_open
        and not planner_state.get(
            "bootstrap_confirmation_applied"
        )
    ):
        eligible_symbols = set(target_weights)

        if bootstrap_eligible_symbols is not None:
            eligible_symbols &= {
                _norm_symbol(symbol)
                for symbol in bootstrap_eligible_symbols
                if _norm_symbol(symbol)
            }

        bootstrapped = []

        for symbol in sorted(eligible_symbols):
            if (
                safe_int(
                    bar_counts.get(symbol, 0),
                    0,
                )
                < min_bar_count
            ):
                continue

            seen_counts[symbol] = max(
                safe_int(
                    seen_counts.get(symbol, 0),
                    0,
                ),
                L3_REQUIRE_TARGET_CONFIRMATION_CYCLES,
            )
            bootstrapped.append(symbol)

        planner_state[
            "bootstrap_confirmation_applied"
        ] = True
        planner_state[
            "bootstrap_confirmation_symbols"
        ] = bootstrapped

    created_dt = datetime.now(timezone.utc)
    safe_source = planner_source.replace(
        " ",
        "_",
    )

    (
        plan_id,
        created_at,
        expires_at,
        ttl_seconds,
    ) = _build_plan_ids(
        cycle_id,
        created_dt,
    )

    plan_id = plan_id.replace(
        "L3-",
        f"L3S-{safe_source}-",
        1,
    )

    built = build_layer3_plan_from_snapshots(
        planner_source=planner_source,
        target=target,
        account=account,
        positions=positions,
        ranked_prices=ranked_prices,
        seen_counts=seen_counts,
        absent_counts=absent_counts,
        cycle_id=cycle_id,
        plan_id=plan_id,
        plan_created_at=created_at,
        plan_expires_at=expires_at,
        plan_ttl_seconds=ttl_seconds,
        open_order_symbols=open_order_symbols,
        open_order_details=open_order_details,
        fail_safe_active=fail_safe_active,
        opening_transition=opening_transition,
        last_trade_prices=last_trade_prices,
    )

    summary = {
        "status": "ok",
        "dry_run": True,
        "shadow_only": True,
        "planner_source": planner_source,
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "timestamp": created_at,
        "plan_created_at": created_at,
        "plan_expires_at": expires_at,
        "plan_ttl_seconds": ttl_seconds,
        "market_is_open": bool(market_is_open),
        "confirmation_updates_allowed": (
            planner_state.get(
                "confirmation_updates_allowed"
            )
        ),
        "confirmation_updates_blocked_reason": (
            planner_state.get(
                "confirmation_updates_blocked_reason"
            )
        ),
        "bootstrap_confirmation_applied": (
            planner_state.get(
                "bootstrap_confirmation_applied",
                False,
            )
        ),
        "bootstrap_confirmation_symbols": (
            planner_state.get(
                "bootstrap_confirmation_symbols",
                [],
            )
        ),
        "bootstrap_confirmation_eligible_symbols": (
            sorted(bootstrap_eligible_symbols)
            if bootstrap_eligible_symbols
            is not None
            else None
        ),
        "open_session_date": (
            open_session_info.get("date")
        ),
        "open_session_live_cycle_count": (
            open_session_info.get(
                "live_cycle_count"
            )
        ),
        "opening_transition_active": (
            opening_transition.get("active")
        ),
        "opening_transition_cycles": (
            opening_transition.get(
                "transition_cycles"
            )
        ),
        "equity": round(
            safe_float(
                account.get("equity"),
                0.0,
            ),
            2,
        ),
        "cash": round(
            safe_float(
                account.get("cash"),
                0.0,
            ),
            2,
        ),
        "estimated_cash_after_plan": (
            built.get(
                "estimated_cash_after_plan"
            )
        ),
        "target_symbol_count": len(
            built.get("target_weights", {})
        ),
        "target_cash_pct": round(
            safe_float(
                built.get("target_cash_pct"),
                0.0,
            ),
            6,
        ),
        "decision_counts": built.get(
            "decision_counts",
            {},
        ),
        "plan_count": len(
            built.get("plan", [])
        ),
        "fail_safe_active": bool(
            fail_safe_active
        ),
    }

    planner_state["last_cycle_id"] = cycle_id
    planner_state["last_run_at"] = created_at
    planner_state["last_plan"] = built.get(
        "plan",
        [],
    )
    planner_state["last_summary"] = summary

    return {
        "plan": built.get("plan", []),
        "summary": summary,
    }


def _prepare_market_open_session_state(
    rebalance: dict,
    *,
    market_is_open: bool,
) -> dict:
    """
    Reset stale confirmation/bootstrap state once per market-open session.

    This prevents yesterday's target_seen/target_absent counters from instantly
    confirming buys/exits on the first live cycle of a new day.
    """
    today = datetime.now(timezone.utc).date().isoformat()

    state = rebalance.setdefault("open_session", {})
    reset_seen_symbols = []
    reset_absent_symbols = []

    if not market_is_open:
        return {
            "date": state.get("date"),
            "is_new_session": False,
            "reset_seen_symbols": reset_seen_symbols,
            "reset_absent_symbols": reset_absent_symbols,
            "live_cycle_count": safe_int(state.get("live_cycle_count", 0), 0),
        }

    if state.get("date") != today:
        seen_counts = rebalance.setdefault("target_seen_counts", {})
        absent_counts = rebalance.setdefault("target_absent_counts", {})

        reset_seen_symbols = sorted([
            symbol
            for symbol, count in seen_counts.items()
            if safe_int(count, 0) != 0
        ])
        reset_absent_symbols = sorted([
            symbol
            for symbol, count in absent_counts.items()
            if safe_int(count, 0) != 0
        ])

        for symbol in list(seen_counts.keys()):
            seen_counts[symbol] = 0

        for symbol in list(absent_counts.keys()):
            absent_counts[symbol] = 0

        rebalance["bootstrap_confirmation_applied"] = False
        rebalance["bootstrap_confirmation_symbols"] = []
        rebalance["bootstrap_confirmation_warmup_filter_applied"] = False
        rebalance["bootstrap_confirmation_warmup_symbols"] = []
        rebalance["bootstrap_confirmation_warmup_skipped_symbols"] = []
        rebalance["bootstrap_confirmation_warmup_stale_symbols"] = []
        rebalance["bootstrap_confirmation_warmup_missing_age_symbols"] = []

        state.clear()
        state["date"] = today
        state["live_cycle_count"] = 0
        state["opened_at"] = datetime.now(timezone.utc).isoformat()

        logging.info(
            "[Layer3Bootstrap] Prepared new open-session confirmation state | "
            "date=%s reset_seen_symbols=%s reset_absent_symbols=%s",
            today,
            reset_seen_symbols,
            reset_absent_symbols,
        )

    state["live_cycle_count"] = safe_int(state.get("live_cycle_count", 0), 0) + 1
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    return {
        "date": state.get("date"),
        "is_new_session": bool(
            reset_seen_symbols
            or reset_absent_symbols
            or state.get("live_cycle_count") == 1
        ),
        "reset_seen_symbols": reset_seen_symbols,
        "reset_absent_symbols": reset_absent_symbols,
        "live_cycle_count": safe_int(state.get("live_cycle_count", 0), 0),
    }


def _opening_transition_info(rebalance: dict, market_is_open: bool) -> dict:
    transition_cycles = _layer3_int_setting(
        "layer3_opening_transition_cycles",
        3,
    )
    open_session = (
        rebalance.get("open_session", {})
        if isinstance(rebalance.get("open_session"), dict)
        else {}
    )
    live_cycle_count = safe_int(open_session.get("live_cycle_count", 0), 0)
    active = bool(
        market_is_open
        and transition_cycles > 0
        and 1 <= live_cycle_count <= transition_cycles
    )

    return {
        "active": active,
        "live_cycle_count": live_cycle_count,
        "transition_cycles": transition_cycles,
    }


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

    If an off-hours Layer 1/2 warmup target exists, bootstrap is restricted to
    symbols that were already present in that warmup target. This reduces
    first-open churn by preventing brand-new open-only symbols from being
    instantly confirmed by bootstrap. Those new symbols can still trade after
    normal target confirmation cycles.
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

    warmup_symbols, warmup_diagnostics = _get_off_hours_warmup_bootstrap_symbols()
    warmup_target_symbols = set(
        warmup_diagnostics.get("warmup_target_symbols", []) or []
    )

    if warmup_target_symbols:
        eligible_target_symbols = set(target_symbols) & warmup_symbols
        warmup_skipped_symbols = sorted(set(target_symbols) - warmup_symbols)
    else:
        eligible_target_symbols = set(target_symbols)
        warmup_skipped_symbols = []

    if warmup_skipped_symbols:
        logging.info(
            "[Layer3Bootstrap] Warmup freshness filter delayed bootstrap for "
            "symbols=%s eligible_warmup_symbols=%s stale_warmup_symbols=%s "
            "missing_age_symbols=%s max_age_minutes=%s",
            warmup_skipped_symbols,
            sorted(warmup_symbols),
            warmup_diagnostics.get("stale_symbols", []),
            warmup_diagnostics.get("missing_age_symbols", []),
            warmup_diagnostics.get("max_age_minutes"),
        )

    target_seen_counts = rebalance.setdefault("target_seen_counts", {})

    bootstrapped_symbols = []

    for symbol in sorted(eligible_target_symbols):
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
    rebalance["bootstrap_confirmation_warmup_filter_applied"] = bool(warmup_target_symbols)
    rebalance["bootstrap_confirmation_warmup_symbols"] = sorted(warmup_symbols)
    rebalance["bootstrap_confirmation_warmup_target_symbols"] = sorted(warmup_target_symbols)
    rebalance["bootstrap_confirmation_warmup_skipped_symbols"] = warmup_skipped_symbols
    rebalance["bootstrap_confirmation_warmup_stale_symbols"] = warmup_diagnostics.get("stale_symbols", [])
    rebalance["bootstrap_confirmation_warmup_missing_age_symbols"] = warmup_diagnostics.get("missing_age_symbols", [])
    rebalance["bootstrap_confirmation_warmup_freshness_available"] = warmup_diagnostics.get("warmup_freshness_available")
    rebalance["bootstrap_confirmation_warmup_max_age_minutes"] = warmup_diagnostics.get("max_age_minutes")

    if bootstrapped_symbols:
        logging.warning(
            "[Layer3Bootstrap] Bootstrapped target confirmation for symbols=%s "
            "required_seen_count=%s min_bar_count=%s warmup_filter_applied=%s "
            "warmup_skipped_symbols=%s stale_warmup_symbols=%s missing_age_symbols=%s",
            bootstrapped_symbols,
            required_seen_count,
            min_bar_count,
            bool(warmup_target_symbols),
            warmup_skipped_symbols,
            warmup_diagnostics.get("stale_symbols", []),
            warmup_diagnostics.get("missing_age_symbols", []),
        )
    else:
        logging.info(
            "[Layer3Bootstrap] Bootstrap completed but no symbols qualified. "
            "warmup_filter_applied=%s warmup_skipped_symbols=%s stale_warmup_symbols=%s "
            "missing_age_symbols=%s",
            bool(warmup_target_symbols),
            warmup_skipped_symbols,
            warmup_diagnostics.get("stale_symbols", []),
            warmup_diagnostics.get("missing_age_symbols", []),
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
    account_source = str(account.get("source") or "")
    broker_snapshot_ok = bool(account.get("broker_snapshot_ok"))

    if account_source != "alpaca_account" or not broker_snapshot_ok:
        summary = {
            "status": "error",
            "dry_run": True,
            "cycle_id": cycle_id,
            "plan_id": plan_id,
            "timestamp": timestamp,
            "plan_expires_at": plan_expires_at,
            "reason": "broker_account_snapshot_unavailable",
            "account_source": account_source,
            "broker_snapshot_ok": broker_snapshot_ok,
            "account_snapshot_error": account.get("account_snapshot_error"),
            "equity": round(equity, 2),
            "cash": round(cash, 2),
        }

        rebalance["last_cycle_id"] = cycle_id
        rebalance["last_run_at"] = timestamp
        rebalance["last_plan"] = []
        rebalance["last_summary"] = summary
        rebalance["last_error"] = "broker_account_snapshot_unavailable"

        logging.warning(
            "[Layer3] Blocking rebalance plan because broker account snapshot is unavailable. "
            "source=%s equity=%s cash=%s error=%s",
            account_source,
            equity,
            cash,
            account.get("account_snapshot_error"),
        )

        return {"plan": [], "summary": summary}

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

    market_is_open_now = get_market_is_open(app_state)

    # Reset stale confirmation/bootstrap state once at the first executable
    # market-open Layer 3 cycle of the session, before this cycle increments
    # seen/absent counts.
    open_session_info = _prepare_market_open_session_state(
        rebalance,
        market_is_open=market_is_open_now,
    )

    opening_transition = _opening_transition_info(
        rebalance,
        market_is_open_now,
    )

    seen_counts, absent_counts = _update_target_stability(
        rebalance,
        target_weights,
        positions,
    )

    market_is_open = bool(rebalance.get("market_is_open", market_is_open_now))
    confirmation_updates_allowed = bool(
        rebalance.get("confirmation_updates_allowed", False)
    )

    target_symbols = set(target_weights.keys())

    if confirmation_updates_allowed:
        _maybe_bootstrap_layer3_confirmation(
            rebalance=rebalance,
            target_symbols=target_symbols,
            required_seen_count=L3_REQUIRE_TARGET_CONFIRMATION_CYCLES,
            market_is_open=market_is_open,
        )

    built_plan = build_layer3_plan_from_snapshots(
        planner_source="REST",
        target=target,
        account=account,
        positions=positions,
        ranked_prices=ranked_prices,
        seen_counts=seen_counts,
        absent_counts=absent_counts,
        cycle_id=cycle_id,
        plan_id=plan_id,
        plan_created_at=plan_created_at,
        plan_expires_at=plan_expires_at,
        plan_ttl_seconds=plan_ttl_seconds,
        open_order_symbols=open_order_symbols,
        open_order_details=open_order_details,
        fail_safe_active=fail_safe_active,
        opening_transition=opening_transition,
        last_trade_prices=app_state.get(
            "last_trade_price_by_symbol",
            {},
        ),
    )

    plan = built_plan.get("plan", [])
    decision_counts = built_plan.get(
        "decision_counts",
        {},
    )
    estimated_cash = safe_float(
        built_plan.get(
            "estimated_cash_after_plan"
        ),
        cash,
    )

    summary = {
        "status": "ok",
        "dry_run": True,
        "planner_source": "REST",
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "timestamp": timestamp,
        "plan_created_at": plan_created_at,
        "plan_expires_at": plan_expires_at,
        "plan_ttl_seconds": plan_ttl_seconds,
        "execution_layer": "layer4",
        "execution_mode": "layer4_direct_compat",

        "open_session_date": open_session_info.get("date"),
        "open_session_live_cycle_count": open_session_info.get("live_cycle_count"),
        "open_session_reset_seen_symbols": open_session_info.get("reset_seen_symbols", []),
        "open_session_reset_absent_symbols": open_session_info.get("reset_absent_symbols", []),
        "opening_transition_active": opening_transition.get("active"),
        "opening_transition_cycles": opening_transition.get("transition_cycles"),

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
        "bootstrap_confirmation_warmup_filter_applied": rebalance.get(
            "bootstrap_confirmation_warmup_filter_applied",
            False,
        ),
        "bootstrap_confirmation_warmup_skipped_symbols": rebalance.get(
            "bootstrap_confirmation_warmup_skipped_symbols",
            [],
        ),
        "bootstrap_confirmation_warmup_stale_symbols": rebalance.get(
            "bootstrap_confirmation_warmup_stale_symbols",
            [],
        ),
        "bootstrap_confirmation_warmup_missing_age_symbols": rebalance.get(
            "bootstrap_confirmation_warmup_missing_age_symbols",
            [],
        ),
        "bootstrap_confirmation_warmup_freshness_available": rebalance.get(
            "bootstrap_confirmation_warmup_freshness_available",
        ),
        "bootstrap_confirmation_warmup_max_age_minutes": rebalance.get(
            "bootstrap_confirmation_warmup_max_age_minutes",
        ),
        "open_session_warmup_symbols": rebalance.get(
            "open_session_warmup_symbols",
            [],
        ),
        "open_session_reset_seen_symbols": rebalance.get(
            "open_session_reset_seen_symbols",
            [],
        ),
        "open_session_reset_absent_symbols": rebalance.get(
            "open_session_reset_absent_symbols",
            [],
        ),

        "target_symbol_count": len(target_weights),
        "target_cash_pct": round(target_cash_pct, 6),
        "target_total_weight": round(sum(target_weights.values()), 6),
        "market_strength": target_meta.get("market_strength"),

        "account_source": account.get("source"),
        "broker_snapshot_ok": account.get("broker_snapshot_ok"),
        "account_snapshot_error": account.get("account_snapshot_error"),
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

    append_layer3_plan_rows(summary, plan)
    append_layer_portfolio_snapshot_rows(summary, plan)

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