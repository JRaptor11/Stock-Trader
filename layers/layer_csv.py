# layers/layer_csv.py

from __future__ import annotations

import csv
import json
import logging
import os
import threading
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Any


_LAYER_CSV_LOCK = threading.Lock()


LAYER_CSV_FILES = {
    "cycles": "layer_cycles.csv",
    "plans": "layer3_plans.csv",
    "orders": "layer4_orders.csv",
    "portfolio-snapshots": "layer_portfolio_snapshots.csv",
}


LAYER_CYCLE_FIELDS = [
    "timestamp",
    "cycle_id",
    "plan_id",
    "status",
    "reason",
    "market_is_open",
    "fresh_count",
    "required_fresh_symbols",
    "ranked_count",
    "top_symbols",
    "target_summary",
    "layer3_status",
    "layer3_decision_counts",
    "layer4_attempted",
    "layer4_submitted",
    "layer4_skipped",
    "layer4_errors",
    "layer4_blocked_reason",
    "layer4_count_integrity_ok",
    "equity",
    "cash",
    "target_cash_pct",
    "market_strength",
]


LAYER3_PLAN_FIELDS = [
    "timestamp",
    "cycle_id",
    "plan_id",
    "row_id",
    "symbol",
    "decision",
    "reason",
    "blocked_by",
    "current_weight",
    "target_weight",
    "delta_weight",
    "relative_drift",
    "current_qty",
    "target_qty",
    "qty_delta",
    "current_value",
    "target_value",
    "delta_value",
    "live_price",
    "price_source",
    "planned_qty",
    "planned_notional",
    "max_authorized_qty",
    "max_authorized_notional",
    "remaining_authorized_qty",
    "remaining_authorized_notional",
    "cash_before_estimate",
    "cash_after_estimate",
    "target_seen_count",
    "target_absent_count",

    "bootstrap_confirmation_applied",
    "bootstrap_confirmation_symbols",
    "bootstrap_confirmation_warmup_filter_applied",
    "bootstrap_confirmation_warmup_skipped_symbols",
    "open_session_warmup_symbols",
    "open_session_reset_seen_symbols",
    "open_session_reset_absent_symbols",

    "open_order_exists",
    "equity",
    "cash",
    "buying_power",
    "market_strength",
    "plan_created_at",
    "plan_expires_at",
    "plan_ttl_seconds",
]


LAYER4_ORDER_FIELDS = [
    "timestamp",
    "finished_at",
    "cycle_id",
    "plan_id",
    "row_id",
    "symbol",
    "side",
    "status",
    "qty",
    "notional",
    "price",
    "order_id",
    "reason",
    "error",
    "cash",
    "attempted",
    "submitted",
    "skipped",
    "errors",
    "blocked_reason",
    "duration_seconds",
    "count_integrity_ok",
]


LAYER_PORTFOLIO_FIELDS = [
    "timestamp",
    "cycle_id",
    "plan_id",
    "symbol",
    "qty",
    "target_qty",
    "qty_delta",
    "market_price",
    "market_value",
    "target_value",
    "weight",
    "target_weight",
    "weight_drift",
    "cash",
    "equity",
    "decision",
    "reason",
]


def layer_csv_dir() -> Path:
    """
    Default to the app working directory to match the existing CSV behavior.

    Optional override:
    LAYER_CSV_DIR=/some/path
    """
    return Path(os.getenv("LAYER_CSV_DIR", ".")).resolve()


def layer_csv_path(filename: str) -> Path:
    return layer_csv_dir() / filename


def _csv_value(value: Any) -> str | int | float | bool | None:
    if value is None:
        return ""

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, (dict, list, tuple, set)):
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except Exception:
            return str(value)

    return value


def _append_csv_rows(filename: str, fieldnames: list[str], rows: list[dict]) -> None:
    if not rows:
        return

    path = layer_csv_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)

    with _LAYER_CSV_LOCK:
        write_header = not path.exists() or path.stat().st_size == 0

        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )

            if write_header:
                writer.writeheader()

            for row in rows:
                writer.writerow({
                    key: _csv_value(row.get(key))
                    for key in fieldnames
                })


def read_csv_rows(filename: str, limit: int = 1000) -> dict:
    path = layer_csv_path(filename)

    if not path.exists():
        return {
            "status": "missing",
            "file": filename,
            "path": str(path),
            "count": 0,
            "rows": [],
        }

    limit = max(1, min(int(limit or 1000), 10000))
    rows = deque(maxlen=limit)
    total = 0

    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            total += 1
            rows.append(dict(row))

    return {
        "status": "ok",
        "file": filename,
        "path": str(path),
        "count": total,
        "returned": len(rows),
        "rows": list(rows),
    }


def append_layer3_plan_rows(summary: dict | None, plan: list[dict] | None) -> None:
    summary = summary or {}
    plan = plan or []

    rows = []
    for row in plan:
        if not isinstance(row, dict):
            continue

        rows.append({
            "timestamp": row.get("timestamp") or summary.get("timestamp"),
            "cycle_id": row.get("cycle_id") or summary.get("cycle_id"),
            "plan_id": row.get("plan_id") or summary.get("plan_id"),
            "row_id": row.get("row_id"),
            "symbol": row.get("symbol"),
            "decision": row.get("decision"),
            "reason": row.get("reason"),
            "blocked_by": row.get("blocked_by"),
            "current_weight": row.get("current_weight"),
            "target_weight": row.get("target_weight"),
            "delta_weight": row.get("delta_weight"),
            "relative_drift": row.get("relative_drift"),
            "current_qty": row.get("current_qty"),
            "target_qty": row.get("target_qty"),
            "qty_delta": row.get("qty_delta"),
            "current_value": row.get("current_value"),
            "target_value": row.get("target_value"),
            "delta_value": row.get("delta_value"),
            "live_price": row.get("live_price"),
            "price_source": row.get("price_source"),
            "planned_qty": row.get("planned_qty"),
            "planned_notional": row.get("planned_notional"),
            "max_authorized_qty": row.get("max_authorized_qty"),
            "max_authorized_notional": row.get("max_authorized_notional"),
            "remaining_authorized_qty": row.get("remaining_authorized_qty"),
            "remaining_authorized_notional": row.get("remaining_authorized_notional"),
            "cash_before_estimate": row.get("cash_before_estimate"),
            "cash_after_estimate": row.get("cash_after_estimate"),
            "target_seen_count": row.get("target_seen_count"),
            "target_absent_count": row.get("target_absent_count"),

            "bootstrap_confirmation_applied": summary.get("bootstrap_confirmation_applied"),
            "bootstrap_confirmation_symbols": summary.get("bootstrap_confirmation_symbols"),
            "bootstrap_confirmation_warmup_filter_applied": summary.get("bootstrap_confirmation_warmup_filter_applied"),
            "bootstrap_confirmation_warmup_skipped_symbols": summary.get("bootstrap_confirmation_warmup_skipped_symbols"),
            "open_session_warmup_symbols": summary.get("open_session_warmup_symbols"),
            "open_session_reset_seen_symbols": summary.get("open_session_reset_seen_symbols"),
            "open_session_reset_absent_symbols": summary.get("open_session_reset_absent_symbols"),

            "open_order_exists": row.get("open_order_exists"),
            "equity": row.get("equity") or summary.get("equity"),
            "cash": row.get("cash") or summary.get("cash"),
            "buying_power": row.get("buying_power"),
            "market_strength": row.get("market_strength") or summary.get("market_strength"),
            "plan_created_at": row.get("plan_created_at") or summary.get("plan_created_at"),
            "plan_expires_at": row.get("plan_expires_at") or summary.get("plan_expires_at"),
            "plan_ttl_seconds": row.get("plan_ttl_seconds") or summary.get("plan_ttl_seconds"),
        })

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["plans"],
            LAYER3_PLAN_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append Layer 3 plan rows.", exc_info=True)


def append_layer_portfolio_snapshot_rows(summary: dict | None, plan: list[dict] | None) -> None:
    """
    One row per symbol per Layer 3 cycle.

    This is derived from Layer 3 plan rows, so it works without another broker call.
    """
    summary = summary or {}
    plan = plan or []

    rows = []
    for row in plan:
        if not isinstance(row, dict):
            continue

        rows.append({
            "timestamp": row.get("timestamp") or summary.get("timestamp"),
            "cycle_id": row.get("cycle_id") or summary.get("cycle_id"),
            "plan_id": row.get("plan_id") or summary.get("plan_id"),
            "symbol": row.get("symbol"),
            "qty": row.get("current_qty"),
            "target_qty": row.get("target_qty"),
            "qty_delta": row.get("qty_delta"),
            "market_price": row.get("live_price"),
            "market_value": row.get("current_value"),
            "target_value": row.get("target_value"),
            "weight": row.get("current_weight"),
            "target_weight": row.get("target_weight"),
            "weight_drift": row.get("delta_weight"),
            "cash": row.get("cash") or summary.get("cash"),
            "equity": row.get("equity") or summary.get("equity"),
            "decision": row.get("decision"),
            "reason": row.get("reason"),
        })

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["portfolio-snapshots"],
            LAYER_PORTFOLIO_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append Layer portfolio snapshot rows.", exc_info=True)


def append_layer4_order_rows(result: dict | None) -> None:
    result = result or {}
    orders = result.get("orders", []) or []

    rows = []
    for order in orders:
        if not isinstance(order, dict):
            continue

        rows.append({
            "timestamp": datetime.utcnow().isoformat(),
            "finished_at": result.get("finished_at"),
            "cycle_id": result.get("cycle_id"),
            "plan_id": result.get("plan_id"),
            "row_id": order.get("row_id"),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "status": order.get("status"),
            "qty": order.get("qty"),
            "notional": order.get("notional"),
            "price": order.get("price"),
            "order_id": order.get("order_id"),
            "reason": order.get("reason"),
            "error": order.get("error"),
            "cash": order.get("cash"),
            "attempted": result.get("attempted"),
            "submitted": result.get("submitted"),
            "skipped": result.get("skipped"),
            "errors": result.get("errors"),
            "blocked_reason": result.get("blocked_reason"),
            "duration_seconds": result.get("duration_seconds"),
            "count_integrity_ok": result.get("count_integrity_ok"),
        })

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["orders"],
            LAYER4_ORDER_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append Layer 4 order rows.", exc_info=True)


def append_layer_cycle_row(
    *,
    status: str,
    reason: str | None = None,
    market_is_open: bool | None = None,
    fresh_count: int | None = None,
    required_fresh_symbols: int | None = None,
    ranked_count: int | None = None,
    top_symbols: list[str] | None = None,
    target_summary: dict | None = None,
    layer3_summary: dict | None = None,
    layer4_result: dict | None = None,
) -> None:
    target_summary = target_summary or {}
    layer3_summary = layer3_summary or {}
    layer4_result = layer4_result or {}

    row = {
        "timestamp": datetime.utcnow().isoformat(),
        "cycle_id": layer3_summary.get("cycle_id") or layer4_result.get("cycle_id"),
        "plan_id": layer3_summary.get("plan_id") or layer4_result.get("plan_id"),
        "status": status,
        "reason": reason,
        "market_is_open": market_is_open,
        "fresh_count": fresh_count,
        "required_fresh_symbols": required_fresh_symbols,
        "ranked_count": ranked_count,
        "top_symbols": top_symbols,
        "target_summary": target_summary,
        "layer3_status": layer3_summary.get("status"),
        "layer3_decision_counts": layer3_summary.get("decision_counts"),
        "layer4_attempted": layer4_result.get("attempted"),
        "layer4_submitted": layer4_result.get("submitted"),
        "layer4_skipped": layer4_result.get("skipped"),
        "layer4_errors": layer4_result.get("errors"),
        "layer4_blocked_reason": layer4_result.get("blocked_reason"),
        "layer4_count_integrity_ok": layer4_result.get("count_integrity_ok"),
        "equity": layer3_summary.get("equity"),
        "cash": layer3_summary.get("cash"),
        "target_cash_pct": layer3_summary.get("target_cash_pct") or target_summary.get("cash_pct"),
        "market_strength": layer3_summary.get("market_strength") or target_summary.get("market_strength"),
    }

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["cycles"],
            LAYER_CYCLE_FIELDS,
            [row],
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append Layer cycle row.", exc_info=True)