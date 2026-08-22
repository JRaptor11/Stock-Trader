from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _order_on_trade_date(order: dict, trade_date: str) -> bool:
    for key in ("filled_at", "submitted_at"):
        timestamp = _parse_timestamp(order.get(key))
        if (
            timestamp is not None
            and timestamp.astimezone(EASTERN).date().isoformat()
            == trade_date
        ):
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
    fail_safe_observation_rows: list[dict] | None = None,
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
    after_hours = snapshots.get("after_hours_close", {}) or {}
    after_hours_positions = _position_map(
        after_hours.get("positions", []) or []
    )
    close_prices = {
        symbol: _safe_float(row.get("current_price"))
        for symbol, row in close_positions.items()
    }
    close_prices.update({
        str(row.get("symbol") or "").upper(): _safe_float(row.get("price"))
        for row in (closing.get("traded_symbol_prices", []) or [])
        if row.get("symbol") and _safe_float(row.get("price")) > 0
    })
    execution_rows = execution_rows or []
    plan_rows = plan_rows or []
    fail_safe_observation_rows = fail_safe_observation_rows or []
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
    reversal_events = []
    previous_order_by_symbol: dict[str, dict] = {}
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
        close_price = close_prices.get(symbol, 0.0)
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
        previous_order = previous_order_by_symbol.get(symbol)
        is_direction_reversal = bool(
            previous_order and previous_order.get("side") != side
        )
        enriched_order = {
            "order_index": order_index,
            "order_id": order.get("id"),
            "symbol": symbol,
            "side": side,
            "filled_qty": qty,
            "filled_avg_price": price,
            "filled_at": (
                order.get("filled_at")
                or order.get("submitted_at")
            ),
            "filled_notional": notional,
            "is_follow_up": is_follow_up,
            "mark_to_close_pnl": mark_to_close,
            "trade_attribution": attribution,
            "trade_attribution_detail": (
                execution.get("trade_attribution_detail")
                or "unknown"
            ),
            "trade_attribution_evidence": execution.get(
                "trade_attribution_evidence"
            ),
            "is_direction_reversal": is_direction_reversal,
        }
        enriched_orders.append(enriched_order)
        if is_direction_reversal:
            current_at = _parse_timestamp(enriched_order["filled_at"])
            previous_at = _parse_timestamp(previous_order.get("filled_at"))
            seconds_since_previous = (
                (current_at - previous_at).total_seconds()
                if current_at is not None and previous_at is not None else None
            )
            previous_price = _safe_float(previous_order.get("filled_avg_price"))
            reversal_events.append({
                "symbol": symbol,
                "previous_side": previous_order.get("side"),
                "reversal_side": side,
                "previous_filled_at": previous_order.get("filled_at"),
                "reversal_filled_at": enriched_order["filled_at"],
                "seconds_since_previous_order": seconds_since_previous,
                "previous_fill_price": previous_price,
                "reversal_fill_price": price,
                "price_change_since_previous_fill_pct": (
                    (price - previous_price) / previous_price * 100
                    if previous_price > 0 else None
                ),
                "reversal_notional": notional,
                "reversal_mark_to_close_pnl": mark_to_close,
                "trade_attribution": attribution,
                "trade_attribution_detail": enriched_order["trade_attribution_detail"],
                "rapid_reversal_within_30m": bool(
                    seconds_since_previous is not None
                    and 0 <= seconds_since_previous <= 1800
                ),
            })
        previous_order_by_symbol[symbol] = enriched_order
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
            "net_position_unchanged": (
                abs(
                    _safe_float(open_positions.get(symbol, {}).get("qty"))
                    - reported_close_qty
                ) <= 0.000001
            ),
            "realized_pnl_after_cost_sensitivity": [
                {
                    "basis_points": basis_points,
                    "estimated_cost": gross * basis_points / 10000,
                    "realized_pnl_after_cost": (
                        metric["realized_pnl_estimate"]
                        - gross * basis_points / 10000
                    ),
                    "net_pnl_per_gross_dollar": (
                        (
                            metric["realized_pnl_estimate"]
                            - gross * basis_points / 10000
                        ) / gross
                        if gross > 0 else None
                    ),
                }
                for basis_points in (1, 5, 10, 20)
            ],
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
    attribution_detail_metrics: dict[str, dict] = defaultdict(
        lambda: {"order_count": 0, "gross_notional": 0.0, "mark_to_close_pnl": 0.0}
    )
    for row in enriched_orders:
        metric = attribution_metrics[row["trade_attribution"]]
        metric["order_count"] += 1
        metric["gross_notional"] += row["filled_notional"]
        if row["mark_to_close_pnl"] is not None:
            metric["mark_to_close_pnl"] += row["mark_to_close_pnl"]
        detail_metric = attribution_detail_metrics[
            row["trade_attribution_detail"]
        ]
        detail_metric["order_count"] += 1
        detail_metric["gross_notional"] += row["filled_notional"]
        if row["mark_to_close_pnl"] is not None:
            detail_metric["mark_to_close_pnl"] += row["mark_to_close_pnl"]

    follow_up_counterfactuals = []
    threshold_specs = [
        ("fixed_notional", 1000.0),
        ("fixed_notional", 2500.0),
        ("fixed_notional", 5000.0),
    ]
    if average_equity > 0:
        threshold_specs.extend([
            ("average_equity_pct_1", average_equity * 0.01),
            ("average_equity_pct_2_5", average_equity * 0.025),
        ])
    for threshold_type, threshold in threshold_specs:
        ignored = [
            row for row in enriched_orders
            if row["is_follow_up"] and row["filled_notional"] <= threshold
        ]
        follow_up_counterfactuals.append({
            "threshold_type": threshold_type,
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
    after_hours_account = after_hours.get("account", {}) or {}
    after_hours_equity = _safe_float(after_hours_account.get("equity"))
    after_hours_symbol_attribution = []
    for symbol in sorted(set(close_positions) | set(after_hours_positions)):
        close_row = close_positions.get(symbol, {})
        later_row = after_hours_positions.get(symbol, {})
        qty = _safe_float(close_row.get("qty"))
        close_price = _safe_float(close_row.get("current_price"))
        later_price = _safe_float(later_row.get("current_price"))
        after_hours_symbol_attribution.append({
            "symbol": symbol,
            "close_qty": qty,
            "regular_close_price": close_price or None,
            "after_hours_close_price": later_price or None,
            "estimated_after_hours_pnl": (
                qty * (later_price - close_price)
                if qty and close_price and later_price else None
            ),
        })

    threshold_comparison = []
    observations_by_symbol: dict[str, list[dict]] = defaultdict(list)
    for row in fail_safe_observation_rows:
        symbol = str(row.get("symbol") or "").upper()
        timestamp = _parse_timestamp(row.get("timestamp"))
        if (
            symbol
            and timestamp is not None
            and timestamp.astimezone(EASTERN).date().isoformat()
            == trade_date
        ):
            observations_by_symbol[symbol].append(row)

    for rows in observations_by_symbol.values():
        rows.sort(key=lambda row: str(row.get("timestamp") or ""))

    fail_safe_exits: dict[str, list[dict]] = defaultdict(list)
    for row in enriched_orders:
        if (
            row.get("side") == "sell"
            and row.get("trade_attribution_detail")
            == "forced_risk_liquidation"
        ):
            fail_safe_exits[row["symbol"]].append(row)
    for rows in fail_safe_exits.values():
        rows.sort(key=lambda row: str(row.get("filled_at") or ""))

    for threshold in (2, 3, 4, 5):
        crossing_field = f"confirmed_crossing_{threshold}_percent"
        symbols = []
        episodes = []
        for symbol, rows in sorted(observations_by_symbol.items()):
            crossings = [
                row
                for row in rows
                if str(row.get(crossing_field) or "").lower()
                in {"true", "1", "yes"}
            ]
            if not crossings:
                continue

            active_episode = None
            previous_confirmed = False
            for observation in rows:
                confirmed = str(
                    observation.get(crossing_field) or ""
                ).lower() in {"true", "1", "yes"}
                if confirmed and not previous_confirmed:
                    active_episode = {
                        "symbol": symbol,
                        "confirmed_crossing_at": observation.get("timestamp"),
                        "position_qty_at_crossing": _safe_float(
                            observation.get("position_qty")
                        ),
                        "entry_price": _safe_float(
                            observation.get("entry_price")
                        ),
                        "crossing_price": _safe_float(
                            observation.get("current_price")
                        ),
                        "crossing_loss_percent": _safe_float(
                            observation.get("loss_percent")
                        ),
                        "maximum_loss_percent_before_recovery": _safe_float(
                            observation.get("loss_percent")
                        ),
                        "recovered_at": None,
                    }
                    episodes.append(active_episode)
                elif confirmed and active_episode is not None:
                    active_episode["maximum_loss_percent_before_recovery"] = max(
                        active_episode["maximum_loss_percent_before_recovery"],
                        _safe_float(observation.get("loss_percent")),
                    )
                elif not confirmed and previous_confirmed and active_episode is not None:
                    active_episode["recovered_at"] = observation.get("timestamp")
                    active_episode["recovery_price"] = _safe_float(
                        observation.get("current_price")
                    )
                    active_episode = None
                previous_confirmed = confirmed
            crossing = crossings[0]
            crossed_at = _parse_timestamp(crossing.get("timestamp"))
            quantity = _safe_float(crossing.get("position_qty"))
            exit_price = _safe_float(crossing.get("current_price"))
            close_price = close_prices.get(symbol, 0.0)
            actual_exit = next(
                (
                    row
                    for row in fail_safe_exits.get(symbol, [])
                    if (
                        crossed_at is None
                        or _parse_timestamp(row.get("filled_at")) is None
                        or _parse_timestamp(row.get("filled_at"))
                        >= crossed_at
                    )
                ),
                None,
            )
            actual_exit_price = _safe_float(
                (actual_exit or {}).get("filled_avg_price")
            )
            symbols.append({
                "symbol": symbol,
                "confirmed_crossing_at": crossing.get("timestamp"),
                "position_qty_at_crossing": quantity,
                "entry_price": _safe_float(
                    crossing.get("entry_price")
                ),
                "hypothetical_exit_price": exit_price,
                "observed_loss_percent": _safe_float(
                    crossing.get("loss_percent")
                ),
                "maximum_observed_loss_percent": max(
                    _safe_float(row.get("loss_percent"))
                    for row in rows
                ),
                "regular_close_price": close_price or None,
                "hypothetical_pnl_vs_regular_close": (
                    quantity * (exit_price - close_price)
                    if quantity > 0
                    and exit_price > 0
                    and close_price > 0
                    else None
                ),
                "actual_fail_safe_exit_at": (
                    (actual_exit or {}).get("filled_at")
                ),
                "actual_fail_safe_exit_price": (
                    actual_exit_price or None
                ),
                "hypothetical_pnl_vs_actual_fail_safe_exit": (
                    quantity * (exit_price - actual_exit_price)
                    if quantity > 0
                    and exit_price > 0
                    and actual_exit_price > 0
                    else None
                ),
            })

        vs_close_values = [
            row["hypothetical_pnl_vs_regular_close"]
            for row in symbols
            if row["hypothetical_pnl_vs_regular_close"] is not None
        ]
        vs_actual_values = [
            row["hypothetical_pnl_vs_actual_fail_safe_exit"]
            for row in symbols
            if row[
                "hypothetical_pnl_vs_actual_fail_safe_exit"
            ] is not None
        ]
        threshold_comparison.append({
            "threshold_percent": float(threshold),
            "data_source": (
                "intraday_position_loss_observations"
                if observations_by_symbol
                else "no_intraday_observations"
            ),
            "symbols_observed": len(observations_by_symbol),
            "observation_count": sum(
                len(rows) for rows in observations_by_symbol.values()
            ),
            "confirmed_crossing_count": len(symbols),
            "confirmed_crossings": symbols,
            "distinct_episode_count": len(episodes),
            "distinct_episodes": episodes,
            "recovered_episode_count": sum(
                bool(row.get("recovered_at")) for row in episodes
            ),
            "estimated_total_pnl_vs_regular_close": (
                sum(vs_close_values) if vs_close_values else None
            ),
            "estimated_total_pnl_vs_actual_fail_safe_exit": (
                sum(vs_actual_values) if vs_actual_values else None
            ),
            "measurement_only": True,
        })
    earnings_calendar = closing.get("earnings_calendar", {}) or {}
    event_exposure = []
    for symbol, row in close_positions.items():
        event_date = earnings_calendar.get(symbol)
        market_value = abs(_safe_float(row.get("market_value")))
        event_exposure.append({
            "symbol": symbol,
            "market_value": market_value,
            "portfolio_equity_pct": (
                market_value / close_equity * 100
                if close_equity > 0 else None
            ),
            "earnings_date": event_date,
            "earnings_on_trade_date": event_date == trade_date,
        })

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
                close_prices=close_prices,
            )
        ),
        "small_follow_up_counterfactuals": follow_up_counterfactuals,
        "trade_attribution": dict(attribution_metrics),
        "trade_attribution_detail": dict(attribution_detail_metrics),
        "direction_reversal_diagnostics": {
            "event_count": len(reversal_events),
            "rapid_reversal_within_30m_count": sum(
                row["rapid_reversal_within_30m"] for row in reversal_events
            ),
            "reversal_notional": sum(row["reversal_notional"] for row in reversal_events),
            "reversal_mark_to_close_pnl": sum(
                row["reversal_mark_to_close_pnl"] or 0 for row in reversal_events
            ),
            "by_symbol": [
                {
                    "symbol": symbol,
                    "reversal_count": len(symbol_events),
                    "rapid_reversal_within_30m_count": sum(
                        row["rapid_reversal_within_30m"] for row in symbol_events
                    ),
                    "reversal_notional": sum(row["reversal_notional"] for row in symbol_events),
                    "reversal_mark_to_close_pnl": sum(
                        row["reversal_mark_to_close_pnl"] or 0 for row in symbol_events
                    ),
                }
                for symbol in sorted({row["symbol"] for row in reversal_events})
                for symbol_events in [[row for row in reversal_events if row["symbol"] == symbol]]
            ],
            "events": reversal_events,
            "method": (
                "A reversal is a filled order whose side differs from the previous "
                "fill for that symbol; P&L marks the reversal fill to the close."
            ),
        },
        "fill_attribution": enriched_orders,
        "after_hours_attribution": {
            "available": bool(after_hours),
            "regular_close_equity": close_equity or None,
            "after_hours_close_equity": after_hours_equity or None,
            "equity_change_after_hours": (
                after_hours_equity - close_equity
                if after_hours_equity > 0 and close_equity > 0
                else None
            ),
            "symbols": after_hours_symbol_attribution,
        },
        "position_loss_threshold_comparison": threshold_comparison,
        "after_hours_event_exposure": {
            "earnings_calendar_configured": bool(earnings_calendar),
            "positions": event_exposure,
            "earnings_day_market_value": sum(
                row["market_value"]
                for row in event_exposure
                if row["earnings_on_trade_date"]
            ),
        },
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
