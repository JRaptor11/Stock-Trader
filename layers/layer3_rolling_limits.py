from __future__ import annotations

from datetime import datetime, timezone

from utils.numeric import safe_float, safe_int


def _parse_utc_datetime(value) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if value:
        try:
            parsed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.astimezone(timezone.utc)

        except Exception:
            pass

    return datetime.now(timezone.utc)


def _sync_authorized_aliases(row: dict) -> None:
    qty = safe_float(row.get("planned_qty"), 0.0)
    notional = safe_float(
        row.get("planned_notional"),
        0.0,
    )

    row["max_authorized_qty"] = qty
    row["max_authorized_notional"] = notional
    row["remaining_authorized_qty"] = qty
    row["remaining_authorized_notional"] = notional


def _defer(row: dict, *, reason: str) -> None:
    row["decision"] = "SKIP"
    row["reason"] = f"{reason}_deferred"
    row["planned_qty"] = 0.0
    row["planned_notional"] = 0.0
    row["blocked_by"] = (
        list(row.get("blocked_by", []))
        + [reason]
    )
    row["rolling_limit_blocked_reason"] = reason

    _sync_authorized_aliases(row)


def _qty_for_notional(
    notional: float,
    price: float,
    *,
    whole_shares_only: bool,
) -> float:
    if notional <= 0 or price <= 0:
        return 0.0

    raw_qty = notional / price

    if whole_shares_only:
        return float(int(raw_qty))

    return raw_qty


def _prune_history(
    planner_state: dict,
    *,
    now_dt: datetime,
    window_seconds: int,
) -> list[dict]:
    raw_history = planner_state.setdefault(
        "rolling_trade_limit_history",
        [],
    )

    cutoff = (
        now_dt.timestamp()
        - float(window_seconds)
    )

    history = []

    for item in raw_history:
        if not isinstance(item, dict):
            continue

        item_dt = _parse_utc_datetime(
            item.get("timestamp")
        )

        # An authorization exactly one full window old
        # no longer consumes the current window.
        if item_dt.timestamp() > cutoff:
            history.append(item)

    planner_state[
        "rolling_trade_limit_history"
    ] = history

    return history


def _usage(history: list[dict]) -> dict:
    return {
        "trades": sum(
            safe_int(item.get("trades"), 0)
            for item in history
        ),
        "buys": sum(
            safe_int(item.get("buys"), 0)
            for item in history
        ),
        "sells": sum(
            safe_int(item.get("sells"), 0)
            for item in history
        ),
        "buy_notional": round(
            sum(
                safe_float(
                    item.get("buy_notional"),
                    0.0,
                )
                for item in history
            ),
            2,
        ),
        "sell_notional": round(
            sum(
                safe_float(
                    item.get("sell_notional"),
                    0.0,
                )
                for item in history
            ),
            2,
        ),
        "gross_notional": round(
            sum(
                safe_float(
                    item.get("gross_notional"),
                    0.0,
                )
                for item in history
            ),
            2,
        ),
    }


def _count_limit_reason(
    *,
    decision: str,
    usage: dict,
    limits: dict,
) -> str | None:
    if usage["trades"] >= limits["max_trades"]:
        return "rolling_trade_count_limit"

    if (
        decision == "BUY"
        and usage["buys"] >= limits["max_buys"]
    ):
        return "rolling_buy_count_limit"

    if (
        decision == "SELL"
        and usage["sells"] >= limits["max_sells"]
    ):
        return "rolling_sell_count_limit"

    return None


def _notional_remaining(
    *,
    decision: str,
    usage: dict,
    limits: dict,
) -> tuple[float, str]:
    gross_remaining = max(
        0.0,
        limits["max_gross_notional"]
        - usage["gross_notional"],
    )

    if decision == "BUY":
        side_remaining = max(
            0.0,
            limits["max_buy_notional"]
            - usage["buy_notional"],
        )
        side_reason = (
            "rolling_buy_notional_limit"
        )

    else:
        side_remaining = max(
            0.0,
            limits["max_sell_notional"]
            - usage["sell_notional"],
        )
        side_reason = (
            "rolling_sell_notional_limit"
        )

    if side_remaining <= gross_remaining:
        return side_remaining, side_reason

    return (
        gross_remaining,
        "rolling_gross_notional_limit",
    )


def apply_rolling_trade_limits(
    plan: list[dict],
    *,
    planner_state: dict,
    plan_created_at: str,
    enabled: bool,
    active: bool,
    window_seconds: int,
    max_trades: int,
    max_buys: int,
    max_sells: int,
    max_buy_notional: float,
    max_sell_notional: float,
    max_gross_notional: float,
    min_trade_value: float,
    whole_shares_only: bool,
) -> tuple[list[dict], dict]:
    limits = {
        "max_trades": max(
            0,
            int(max_trades),
        ),
        "max_buys": max(
            0,
            int(max_buys),
        ),
        "max_sells": max(
            0,
            int(max_sells),
        ),
        "max_buy_notional": max(
            0.0,
            float(max_buy_notional),
        ),
        "max_sell_notional": max(
            0.0,
            float(max_sell_notional),
        ),
        "max_gross_notional": max(
            0.0,
            float(max_gross_notional),
        ),
    }

    window_seconds = max(
        60,
        int(window_seconds),
    )

    now_dt = _parse_utc_datetime(
        plan_created_at
    )

    history = _prune_history(
        planner_state,
        now_dt=now_dt,
        window_seconds=window_seconds,
    )

    usage_before = _usage(history)
    running = dict(usage_before)

    diagnostics = {
        "enabled": bool(enabled),
        "active": bool(enabled and active),
        "window_seconds": window_seconds,
        "limits": dict(limits),
        "usage_before": dict(usage_before),
        "history_entry_count_before": len(
            history
        ),
        "adjusted_count": 0,
        "deferred_count": 0,
    }

    for row in plan:
        decision = str(
            row.get("decision") or ""
        ).upper().strip()

        row.update({
            "rolling_window_seconds": (
                window_seconds
            ),
            "rolling_trades_used_before": (
                running["trades"]
            ),
            "rolling_buys_used_before": (
                running["buys"]
            ),
            "rolling_sells_used_before": (
                running["sells"]
            ),
            "rolling_buy_notional_used_before": round(
                running["buy_notional"],
                2,
            ),
            "rolling_sell_notional_used_before": round(
                running["sell_notional"],
                2,
            ),
            "rolling_gross_notional_used_before": round(
                running["gross_notional"],
                2,
            ),
            "rolling_trade_limit": (
                limits["max_trades"]
            ),
            "rolling_buy_limit": (
                limits["max_buys"]
            ),
            "rolling_sell_limit": (
                limits["max_sells"]
            ),
            "rolling_buy_notional_limit": (
                limits["max_buy_notional"]
            ),
            "rolling_sell_notional_limit": (
                limits["max_sell_notional"]
            ),
            "rolling_gross_notional_limit": (
                limits["max_gross_notional"]
            ),
            "rolling_limit_original_planned_notional": (
                safe_float(
                    row.get("planned_notional"),
                    0.0,
                )
            ),
            "rolling_limit_adjusted": False,
            "rolling_limit_blocked_reason": None,
        })

        if (
            not diagnostics["active"]
            or decision not in {"BUY", "SELL"}
        ):
            continue

        planned_notional = safe_float(
            row.get("planned_notional"),
            0.0,
        )
        price = safe_float(
            row.get("live_price"),
            0.0,
        )

        if planned_notional <= 0 or price <= 0:
            continue

        count_reason = _count_limit_reason(
            decision=decision,
            usage=running,
            limits=limits,
        )

        if count_reason:
            _defer(
                row,
                reason=count_reason,
            )
            diagnostics["deferred_count"] += 1
            continue

        (
            allowed_notional,
            binding_reason,
        ) = _notional_remaining(
            decision=decision,
            usage=running,
            limits=limits,
        )

        if planned_notional > allowed_notional:
            adjusted_qty = _qty_for_notional(
                allowed_notional,
                price,
                whole_shares_only=(
                    whole_shares_only
                ),
            )

            adjusted_notional = (
                adjusted_qty * price
            )

            if (
                adjusted_qty <= 0
                or adjusted_notional
                < min_trade_value
            ):
                _defer(
                    row,
                    reason=binding_reason,
                )
                diagnostics[
                    "deferred_count"
                ] += 1
                continue

            row["planned_qty"] = round(
                adjusted_qty,
                6,
            )
            row["planned_notional"] = round(
                adjusted_notional,
                2,
            )
            row["reason"] = (
                f"{row.get('reason') or decision.lower()}_"
                "rolling_window_adjusted"
            )
            row[
                "rolling_limit_adjusted"
            ] = True
            row[
                "rolling_limit_blocked_reason"
            ] = binding_reason

            _sync_authorized_aliases(row)

            diagnostics[
                "adjusted_count"
            ] += 1

            planned_notional = (
                adjusted_notional
            )

        running["trades"] += 1
        running["gross_notional"] += (
            planned_notional
        )

        if decision == "BUY":
            running["buys"] += 1
            running["buy_notional"] += (
                planned_notional
            )
        else:
            running["sells"] += 1
            running["sell_notional"] += (
                planned_notional
            )

    diagnostics[
        "tentative_usage_after"
    ] = {
        "trades": running["trades"],
        "buys": running["buys"],
        "sells": running["sells"],
        "buy_notional": round(
            running["buy_notional"],
            2,
        ),
        "sell_notional": round(
            running["sell_notional"],
            2,
        ),
        "gross_notional": round(
            running["gross_notional"],
            2,
        ),
    }

    return plan, diagnostics


def finalize_rolling_trade_limits(
    *,
    planner_state: dict,
    plan: list[dict],
    plan_created_at: str,
    cycle_id: int,
    plan_id: str,
    planner_source: str,
    record_history: bool,
    diagnostics: dict,
) -> dict:
    authorized = {
        "trades": 0,
        "buys": 0,
        "sells": 0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "gross_notional": 0.0,
    }

    for row in plan:
        decision = str(
            row.get("decision") or ""
        ).upper().strip()

        notional = safe_float(
            row.get("planned_notional"),
            0.0,
        )

        if (
            decision not in {"BUY", "SELL"}
            or notional <= 0
        ):
            continue

        authorized["trades"] += 1
        authorized["gross_notional"] += (
            notional
        )

        if decision == "BUY":
            authorized["buys"] += 1
            authorized["buy_notional"] += (
                notional
            )
        else:
            authorized["sells"] += 1
            authorized["sell_notional"] += (
                notional
            )

    for key in (
        "buy_notional",
        "sell_notional",
        "gross_notional",
    ):
        authorized[key] = round(
            authorized[key],
            2,
        )

    history = planner_state.setdefault(
        "rolling_trade_limit_history",
        [],
    )

    if (
        record_history
        and authorized["trades"] > 0
    ):
        history.append({
            "timestamp": plan_created_at,
            "cycle_id": cycle_id,
            "plan_id": plan_id,
            "planner_source": planner_source,
            **authorized,
        })

    history = _prune_history(
        planner_state,
        now_dt=_parse_utc_datetime(
            plan_created_at
        ),
        window_seconds=safe_int(
            diagnostics.get(
                "window_seconds"
            ),
            600,
        ),
    )

    diagnostics[
        "authorized_this_cycle"
    ] = authorized

    diagnostics["usage_after"] = _usage(
        history
    )

    diagnostics[
        "history_entry_count_after"
    ] = len(history)

    return diagnostics