from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _order_on_trade_date(order: dict, trade_date: str) -> bool:
    for key in ("filled_at", "submitted_at"):
        value = str(order.get(key) or "")
        if value[:10] == trade_date:
            return True
    return False


def _position_map(rows: list[dict]) -> dict[str, dict]:
    return {
        str(row.get("symbol") or "").upper(): row
        for row in rows
        if row.get("symbol")
    }


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None
            else parsed
        )
    except ValueError:
        return None


def _mark_to_close_pnl(side: str, qty: float, price: float, close: float) -> float | None:
    if qty <= 0 or price <= 0 or close <= 0:
        return None
    return qty * (close - price) * (1 if side == "buy" else -1)


def _plan_interval_counterfactuals(
    plan_rows: list[dict],
    *,
    trade_date: str,
    close_prices: dict[str, float],
) -> list[dict]:
    eligible = []
    for row in plan_rows:
        timestamp = _parse_timestamp(
            row.get("timestamp") or row.get("plan_created_at")
        )
        decision = str(row.get("decision") or "").upper()
        symbol = str(row.get("symbol") or "").upper()
        if (
            timestamp is None
            or timestamp.date().isoformat() != trade_date
            or decision not in {"BUY", "SELL"}
            or not symbol
        ):
            continue
        eligible.append((timestamp, symbol, decision, row))

    results = []
    for minutes in (10, 15, 30):
        selected: dict[tuple[int, str], tuple] = {}
        bucket_seconds = minutes * 60
        for timestamp, symbol, decision, row in eligible:
            bucket = int(timestamp.timestamp()) // bucket_seconds
            selected[(bucket, symbol)] = (timestamp, symbol, decision, row)

        gross = 0.0
        pnl = 0.0
        priced = 0
        for _, symbol, decision, row in selected.values():
            qty = _safe_float(
                row.get("planned_qty")
                or row.get("remaining_authorized_qty")
                or row.get("max_authorized_qty")
                or row.get("qty")
            )
            price = _safe_float(row.get("live_price") or row.get("price"))
            notional = _safe_float(row.get("planned_notional")) or qty * price
            gross += abs(notional)
            estimate = _mark_to_close_pnl(
                decision.lower(), qty, price, close_prices.get(symbol, 0)
            )
            if estimate is not None:
                pnl += estimate
                priced += 1
        results.append({
            "target_update_interval_minutes": minutes,
            "estimated_trade_count": len(selected),
            "estimated_gross_notional": gross,
            "estimated_mark_to_close_pnl": pnl if priced else None,
            "priced_trade_count": priced,
            "method": (
                "Last executable Layer 3 plan row per symbol in each interval; "
                "marked to the closing broker price. This is a replay estimate, "
                "not a full portfolio backtest."
            ),
        })
    return results


def build_execution_analytics(
    snapshots: dict,
    *,
    trade_date: str,
    execution_rows: list[dict] | None = None,
    plan_rows: list[dict] | None = None,
) -> dict:
    """
    Build broker-fill-derived daily execution and turnover measurements.

    This is analysis only. It does not block or modify trading behavior.
    """
    opening = snapshots.get("open", {}) or {}
    closing = snapshots.get("close", {}) or {}
    open_account = opening.get("account", {}) or {}
    close_account = closing.get("account", {}) or {}
    open_positions = _position_map(opening.get("positions", []) or [])
    close_positions = _position_map(closing.get("positions", []) or [])
    execution_rows = execution_rows or []
    plan_rows = plan_rows or []
    execution_by_order_id = {
        str(row.get("order_id")): row
        for row in execution_rows
        if row.get("order_id")
    }

    orders = [
        order
        for order in (closing.get("orders", []) or [])
        if str(order.get("status") or "").lower() == "filled"
        and _safe_float(order.get("filled_qty")) > 0
        and _safe_float(order.get("filled_avg_price")) > 0
        and _order_on_trade_date(order, trade_date)
    ]
    orders.sort(
        key=lambda order: str(
            order.get("filled_at")
            or order.get("submitted_at")
            or ""
        )
    )

    state = {
        symbol: {
            "qty": _safe_float(row.get("qty")),
            "avg_entry_price": _safe_float(row.get("avg_entry_price")),
        }
        for symbol, row in open_positions.items()
    }
    symbol_metrics: dict[str, dict] = defaultdict(lambda: {
        "order_count": 0,
        "buy_order_count": 0,
        "sell_order_count": 0,
        "buy_qty": 0.0,
        "sell_qty": 0.0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "gross_traded_notional": 0.0,
        "realized_pnl_estimate": 0.0,
        "unmatched_sell_qty": 0.0,
        "direction_reversals": 0,
        "first_side": None,
        "last_side": None,
        "first_order_notional": 0.0,
        "follow_up_order_count": 0,
        "follow_up_notional": 0.0,
        "first_order_mark_to_close_pnl": 0.0,
        "follow_up_mark_to_close_pnl": 0.0,
        "mark_to_close_priced_order_count": 0,
    })

    enriched_orders = []
    for order_index, order in enumerate(orders):
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").lower()
        if not symbol or side not in {"buy", "sell"}:
            continue

        qty = _safe_float(order.get("filled_qty"))
        price = _safe_float(order.get("filled_avg_price"))
        notional = qty * price
        metric = symbol_metrics[symbol]
        execution = execution_by_order_id.get(str(order.get("id") or ""), {})
        attribution = (
            execution.get("trade_attribution")
            or order.get("trade_attribution")
            or "unknown"
        )
        close_price = _safe_float(
            close_positions.get(symbol, {}).get("current_price")
        )
        mark_to_close = _mark_to_close_pnl(side, qty, price, close_price)
        is_follow_up = metric["first_side"] is not None
        if mark_to_close is not None:
            metric["mark_to_close_priced_order_count"] += 1
            key = (
                "follow_up_mark_to_close_pnl"
                if is_follow_up
                else "first_order_mark_to_close_pnl"
            )
            metric[key] += mark_to_close
        enriched_orders.append({
            "order_index": order_index,
            "order_id": order.get("id"),
            "symbol": symbol,
            "side": side,
            "filled_qty": qty,
            "filled_avg_price": price,
            "filled_notional": notional,
            "is_follow_up": is_follow_up,
            "mark_to_close_pnl": mark_to_close,
            "trade_attribution": attribution,
            "trade_attribution_evidence": execution.get(
                "trade_attribution_evidence"
            ),
        })
        position = state.setdefault(
            symbol,
            {"qty": 0.0, "avg_entry_price": 0.0},
        )

        if metric["first_side"] is None:
            metric["first_side"] = side
            metric["first_order_notional"] = notional
        else:
            metric["follow_up_order_count"] += 1
            metric["follow_up_notional"] += notional
        if metric["last_side"] and metric["last_side"] != side:
            metric["direction_reversals"] += 1
        metric["last_side"] = side
        metric["order_count"] += 1
        metric["gross_traded_notional"] += notional

        if side == "buy":
            old_qty = position["qty"]
            new_qty = old_qty + qty
            position["avg_entry_price"] = (
                (
                    old_qty * position["avg_entry_price"]
                    + qty * price
                )
                / new_qty
                if new_qty > 0 else 0.0
            )
            position["qty"] = new_qty
            metric["buy_order_count"] += 1
            metric["buy_qty"] += qty
            metric["buy_notional"] += notional
        else:
            matched_qty = min(qty, max(0.0, position["qty"]))
            metric["realized_pnl_estimate"] += matched_qty * (
                price - position["avg_entry_price"]
            )
            metric["unmatched_sell_qty"] += max(0.0, qty - matched_qty)
            position["qty"] = max(0.0, position["qty"] - qty)
            if position["qty"] <= 0:
                position["avg_entry_price"] = 0.0
            metric["sell_order_count"] += 1
            metric["sell_qty"] += qty
            metric["sell_notional"] += notional

    all_symbols = sorted(
        set(open_positions)
        | set(close_positions)
        | set(symbol_metrics)
    )
    open_equity = _safe_float(open_account.get("equity"))
    close_equity = _safe_float(close_account.get("equity"))
    average_equity = (
        (open_equity + close_equity) / 2
        if open_equity > 0 and close_equity > 0
        else open_equity or close_equity
    )
    symbol_rows = []
    reconstructed_mismatches = []
    for symbol in all_symbols:
        metric = symbol_metrics[symbol]
        reconstructed_qty = _safe_float(
            state.get(symbol, {}).get("qty")
        )
        reported_close_qty = _safe_float(
            close_positions.get(symbol, {}).get("qty")
        )
        qty_difference = reconstructed_qty - reported_close_qty
        if abs(qty_difference) > 0.000001:
            reconstructed_mismatches.append({
                "symbol": symbol,
                "reconstructed_qty": reconstructed_qty,
                "reported_close_qty": reported_close_qty,
                "difference": qty_difference,
            })

        gross = metric["gross_traded_notional"]
        one_way_symbol_notional = min(
            metric["buy_notional"], metric["sell_notional"]
        )
        average_symbol_value = (
            abs(_safe_float(open_positions.get(symbol, {}).get("market_value")))
            + abs(_safe_float(close_positions.get(symbol, {}).get("market_value")))
        ) / 2
        symbol_rows.append({
            "symbol": symbol,
            **metric,
            "same_day_round_trip": (
                metric["buy_qty"] > 0
                and metric["sell_qty"] > 0
            ),
            "same_day_round_trip_count": min(
                metric["buy_order_count"],
                metric["sell_order_count"],
            ),
            "one_way_traded_notional": one_way_symbol_notional,
            "net_traded_qty": metric["buy_qty"] - metric["sell_qty"],
            "open_qty": _safe_float(open_positions.get(symbol, {}).get("qty")),
            "reconstructed_close_qty": reconstructed_qty,
            "reported_close_qty": reported_close_qty,
            "quantity_reconciliation_difference": qty_difference,
            "realized_pnl_per_gross_dollar": (
                metric["realized_pnl_estimate"] / gross
                if gross > 0 else None
            ),
            "gross_turnover_pct_of_average_equity": (
                gross / average_equity * 100
                if average_equity > 0 else None
            ),
            "one_way_turnover_pct_of_average_equity": (
                one_way_symbol_notional / average_equity * 100
                if average_equity > 0 else None
            ),
            "gross_turnover_multiple_of_average_position_value": (
                gross / average_symbol_value
                if average_symbol_value > 0 else None
            ),
        })

    buy_notional = sum(row["buy_notional"] for row in symbol_rows)
    sell_notional = sum(row["sell_notional"] for row in symbol_rows)
    gross_notional = buy_notional + sell_notional
    one_way_notional = min(buy_notional, sell_notional)
    realized_estimate = sum(
        row["realized_pnl_estimate"]
        for row in symbol_rows
    )
    open_unrealized = sum(
        _safe_float(row.get("unrealized_pl"))
        for row in open_positions.values()
    )
    close_unrealized = sum(
        _safe_float(row.get("unrealized_pl"))
        for row in close_positions.values()
    )
    equity_change = (
        close_equity - open_equity
        if open_equity and close_equity else None
    )
    explained_pnl = (
        realized_estimate
        + close_unrealized
        - open_unrealized
    )
    last_equity = _safe_float(open_account.get("last_equity"))
    close_to_close_return_pct = (
        (close_equity / last_equity - 1) * 100
        if close_equity > 0 and last_equity > 0 else None
    )
    benchmark_excess = []
    for benchmark in closing.get("benchmarks", []) or []:
        benchmark_return = benchmark.get("return_pct")
        benchmark_excess.append({
            "symbol": benchmark.get("symbol"),
            "benchmark_return_pct": benchmark_return,
            "strategy_close_to_close_return_pct": close_to_close_return_pct,
            "strategy_excess_return_percentage_points": (
                close_to_close_return_pct - _safe_float(benchmark_return)
                if close_to_close_return_pct is not None
                and benchmark_return is not None
                else None
            ),
        })

    attribution_metrics: dict[str, dict] = defaultdict(
        lambda: {"order_count": 0, "gross_notional": 0.0, "mark_to_close_pnl": 0.0}
    )
    for row in enriched_orders:
        metric = attribution_metrics[row["trade_attribution"]]
        metric["order_count"] += 1
        metric["gross_notional"] += row["filled_notional"]
        if row["mark_to_close_pnl"] is not None:
            metric["mark_to_close_pnl"] += row["mark_to_close_pnl"]

    follow_up_counterfactuals = []
    for threshold in (50, 100, 250, 500):
        ignored = [
            row for row in enriched_orders
            if row["is_follow_up"] and row["filled_notional"] <= threshold
        ]
        follow_up_counterfactuals.append({
            "maximum_follow_up_notional_ignored": threshold,
            "ignored_order_count": len(ignored),
            "ignored_gross_notional": sum(
                row["filled_notional"] for row in ignored
            ),
            "estimated_mark_to_close_pnl_removed": sum(
                row["mark_to_close_pnl"] or 0 for row in ignored
            ),
            "estimated_mark_to_close_pnl_if_ignored": sum(
                row["mark_to_close_pnl"] or 0 for row in enriched_orders
                if row not in ignored
            ),
        })

    first_rows = [row for row in enriched_orders if not row["is_follow_up"]]
    follow_rows = [row for row in enriched_orders if row["is_follow_up"]]

    return {
        "trade_date": trade_date,
        "measurement_only": True,
        "filled_order_count": len(orders),
        "symbols_traded": sum(
            row["order_count"] > 0 for row in symbol_rows
        ),
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "gross_traded_notional": gross_notional,
        "one_way_traded_notional": one_way_notional,
        "average_equity": average_equity or None,
        "gross_trading_intensity_pct": (
            gross_notional / average_equity * 100
            if average_equity > 0 else None
        ),
        "one_way_turnover_pct": (
            one_way_notional / average_equity * 100
            if average_equity > 0 else None
        ),
        "same_day_round_trip_symbol_count": sum(
            row["same_day_round_trip"] for row in symbol_rows
        ),
        "same_day_round_trip_count": sum(
            row["same_day_round_trip_count"] for row in symbol_rows
        ),
        "direction_reversal_count": sum(
            row["direction_reversals"] for row in symbol_rows
        ),
        "follow_up_order_count": sum(
            row["follow_up_order_count"] for row in symbol_rows
        ),
        "follow_up_notional": sum(
            row["follow_up_notional"] for row in symbol_rows
        ),
        "first_vs_follow_up_performance": {
            "first_order_count": len(first_rows),
            "first_order_gross_notional": sum(
                row["filled_notional"] for row in first_rows
            ),
            "first_order_mark_to_close_pnl": sum(
                row["mark_to_close_pnl"] or 0 for row in first_rows
            ),
            "follow_up_order_count": len(follow_rows),
            "follow_up_gross_notional": sum(
                row["filled_notional"] for row in follow_rows
            ),
            "follow_up_mark_to_close_pnl": sum(
                row["mark_to_close_pnl"] or 0 for row in follow_rows
            ),
            "method": "Each fill is marked independently to the closing broker price.",
        },
        "broker_fill_realized_pnl_estimate": realized_estimate,
        "realized_pnl_per_gross_dollar": (
            realized_estimate / gross_notional
            if gross_notional > 0 else None
        ),
        "open_unrealized_pnl": open_unrealized,
        "close_unrealized_pnl": close_unrealized,
        "realized_plus_unrealized_change": explained_pnl,
        "open_to_close_equity_change": equity_change,
        "pnl_reconstruction_gap": (
            equity_change - explained_pnl
            if equity_change is not None else None
        ),
        "overnight_equity_change": (
            open_equity - last_equity
            if open_equity and last_equity else None
        ),
        "close_to_close_return_pct": close_to_close_return_pct,
        "benchmark_excess": benchmark_excess,
        "cost_sensitivity": [
            {
                "basis_points_per_traded_dollar": basis_points,
                "estimated_cost": (
                    gross_notional * basis_points / 10000
                ),
                "open_to_close_pnl_after_estimated_cost": (
                    equity_change
                    - gross_notional * basis_points / 10000
                    if equity_change is not None else None
                ),
            }
            for basis_points in (1, 5, 10, 20)
        ],
        "target_update_interval_counterfactuals": (
            _plan_interval_counterfactuals(
                plan_rows,
                trade_date=trade_date,
                close_prices={
                    symbol: _safe_float(row.get("current_price"))
                    for symbol, row in close_positions.items()
                },
            )
        ),
        "small_follow_up_counterfactuals": follow_up_counterfactuals,
        "trade_attribution": dict(attribution_metrics),
        "fill_attribution": enriched_orders,
        "attribution_coverage": {
            "attributed_order_count": sum(
                row["trade_attribution"] != "unknown"
                for row in enriched_orders
            ),
            "unknown_order_count": sum(
                row["trade_attribution"] == "unknown"
                for row in enriched_orders
            ),
        },
        "symbol_analytics": symbol_rows,
        "quantity_reconstruction_mismatches": reconstructed_mismatches,
        "realized_pnl_method": (
            "Average-cost reconstruction from the opening broker position "
            "snapshot and broker-reported filled order quantities/prices. "
            "This is operational attribution, not tax-lot accounting."
        ),
    }
