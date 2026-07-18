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
    "shadow": "layer4_shadow.csv",
    "portfolio-snapshots": "layer_portfolio_snapshots.csv",
    "live-bar-health": "layer_live_bar_health.csv",

    # Shadow diagnostics for comparing the current delayed REST-bar Layer 1/2
    # pipeline against an independent live-bar Layer 1/2 pipeline.
    "live-strategy-shadow": "layer_live_strategy_shadow.csv",
    "live-strategy-shadow-cycles": "layer_live_strategy_shadow_cycles.csv",
    "live-strategy-outcomes": "layer_live_strategy_outcomes.csv",

    # Full shadow portfolio simulator for direct REST-vs-LIVE comparison.
    "strategy-shadow-orders": "layer_strategy_shadow_orders.csv",
    "strategy-shadow-portfolios": "layer_strategy_shadow_portfolios.csv",
    "strategy-shadow-comparison": "layer_strategy_shadow_comparison.csv",

    # Derived joined attribution views rebuilt from the existing shadow CSVs.
    "rest-live-attribution-cycles": "layer_rest_live_attribution_cycles.csv",
    "rest-live-attribution-symbols": "layer_rest_live_attribution_symbols.csv",

    # Opening-delay diagnostics: what live-only/hybrid would have done while
    # the REST production path was blocked by stale bars.
    "opening-shadow-cycles": "layer_opening_shadow_cycles.csv",
    "opening-shadow-trades": "layer_opening_shadow_trades.csv",
    "opening-shadow-outcomes": "layer_opening_shadow_outcomes.csv",
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

    "account_source",
    "broker_snapshot_ok",
    "account_snapshot_error",

    "confirmation_updates_allowed",
    "confirmation_updates_blocked_reason",

    "open_session_date",
    "open_session_live_cycle_count",
    "open_session_reset_seen_symbols",
    "open_session_reset_absent_symbols",

    "opening_transition_active",
    "opening_transition_cycles",

    "bootstrap_confirmation_applied",
    "bootstrap_confirmation_symbols",
    "bootstrap_confirmation_warmup_filter_applied",
    "bootstrap_confirmation_warmup_symbols",
    "bootstrap_confirmation_warmup_target_symbols",
    "bootstrap_confirmation_warmup_skipped_symbols",
    "bootstrap_confirmation_warmup_stale_symbols",
    "bootstrap_confirmation_warmup_missing_age_symbols",
    "bootstrap_confirmation_warmup_freshness_available",
    "bootstrap_confirmation_warmup_max_age_minutes",

    "layer4_attempted",
    "layer4_submitted",
    "layer4_skipped",
    "layer4_errors",
    "layer4_blocked_reason",
    "layer4_count_integrity_ok",

    "layer4_shadow_rows",
    "layer4_shadow_execute",
    "layer4_shadow_delay",
    "layer4_shadow_reduce",
    "layer4_shadow_block",

    "equity",
    "cash",
    "target_cash_pct",
    "market_strength",
]


LAYER3_PLAN_FIELDS = [
    "timestamp",
    "planner_source",
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
    "submitted_side",
    "order_request_side",
    "order_type",
    "time_in_force",
    "market_is_open",
    "status",
    "qty",
    "notional",
    "price",
    "order_id",
    "reason",
    "error",
    "broker_error_code",
    "broker_error_message",
    "broker_error_existing_qty",
    "broker_error_available_qty",
    "broker_error_held_for_orders",
    "broker_error_symbol",
    "broker_error_raw",
    "cooldown_until",
    "cooldown_remaining_seconds",
    "position_qty_before",
    "open_order_count_for_symbol",
    "cash",
    "cash_budget_before",
    "cash_budget_after",
    "attempted",
    "submitted",
    "skipped",
    "errors",
    "blocked_reason",
    "duration_seconds",
    "count_integrity_ok",
]


LAYER4_SHADOW_FIELDS = [
    "timestamp",
    "finished_at",
    "cycle_id",
    "plan_id",
    "row_id",
    "symbol",
    "side",
    "decision",
    "layer3_reason",
    "shadow_action",
    "shadow_confidence",
    "shadow_reason",
    "would_execute",
    "would_delay",
    "would_reduce",
    "would_block",
    "recommended_qty_multiplier",
    "live_pressure_score",
    "buy_chase_risk",
    "sell_strength_protection",
    "sell_classification",
    "position_green",
    "position_unrealized_plpc",
    "live_tick_count",
    "live_1m_bar_count",
    "live_5m_bar_count",
    "live_price",
    "row_price",
    "live_vs_row_price_pct",
    "latest_1m_close",
    "latest_5m_close",
    "latest_1m_volume",
    "latest_5m_volume",
    "live_ret_30s",
    "live_ret_60s",
    "live_ret_300s",
    "live_range_component",
    "live_volume_component",
    "qty",
    "notional",
    "price",
    "current_qty",
    "target_qty",
    "current_weight",
    "target_weight",
    "delta_weight",
    "relative_drift",
    "shadow_row_count",
    "shadow_execute_count",
    "shadow_delay_count",
    "shadow_reduce_count",
    "shadow_block_count",
    "shadow_error",
    "duration_seconds",
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

LAYER_LIVE_BAR_HEALTH_FIELDS = [
    "timestamp",
    "cycle_id",
    "symbol",
    "market_is_open",
    "tick_count",
    "live_1m_bar_count",
    "live_5m_bar_count",
    "latest_live_price",
    "latest_1m_close",
    "latest_5m_close",
    "latest_1m_volume",
    "latest_5m_volume",
    "rest_bar_count",
    "rest_latest_close",
    "rest_latest_timestamp",
    "rest_bar_age_minutes",
    "live_vs_rest_close_pct",
]


LAYER_LIVE_STRATEGY_SHADOW_FIELDS = [
    "timestamp",
    "cycle_id",
    "symbol",
    "market_is_open",
    "rest_status",
    "live_status",
    "live_timeframe_seconds",
    "live_bar_count",
    "rest_bar_count",
    "rest_rank",
    "live_rank",
    "rank_delta_live_minus_rest",
    "rest_score",
    "live_score",
    "score_delta_live_minus_rest",
    "current_weight",
    "rest_target_weight",
    "live_target_weight",
    "target_weight_delta_live_minus_rest",
    "rest_decision",
    "live_shadow_decision",
    "decision_agreement",
    "live_preference_direction",
    "rest_planned_qty",
    "rest_planned_notional",
    "live_shadow_estimated_notional",
    "live_planner_source",
    "live_planner_status",
    "live_planner_planned_qty",
    "live_planner_target_seen_count",
    "live_planner_target_absent_count",
    "rest_price",
    "live_price",
    "live_vs_rest_price_pct",
    "rest_reason",
    "live_reason",
    "rest_top_symbols",
    "live_top_symbols",
    "rest_target_summary",
    "live_target_summary",
]


LAYER_LIVE_STRATEGY_SHADOW_CYCLE_FIELDS = [
    "timestamp",
    "cycle_id",
    "market_is_open",
    "rest_status",
    "live_status",
    "live_timeframe_seconds",
    "live_min_required_bars",
    "live_symbols_ready",
    "symbol_count",
    "rest_ranked_count",
    "live_ranked_count",
    "rest_top_symbols",
    "live_top_symbols",
    "top5_overlap_count",
    "top5_overlap_symbols",
    "total_abs_target_weight_diff",
    "avg_abs_target_weight_diff",
    "max_abs_target_weight_diff",
    "decision_agreement_rate",
    "rest_buy_count",
    "rest_sell_count",
    "rest_hold_count",
    "live_buy_count",
    "live_sell_count",
    "live_hold_count",
    "live_not_estimated_count",
    "live_cash_pct",
    "rest_cash_pct",
    "live_market_strength",
    "rest_market_strength",
    "live_planner_status",
    "live_planner_decision_counts",
    "live_planner_bootstrap_confirmation_applied",
    "error",
]


LAYER_LIVE_STRATEGY_OUTCOME_FIELDS = [
    "source_timestamp",
    "outcome_timestamp",
    "source_cycle_id",
    "symbol",
    "market_is_open_at_source",
    "rest_status",
    "live_status",
    "live_timeframe_seconds",
    "rest_rank",
    "live_rank",
    "rest_score",
    "live_score",
    "current_weight",
    "rest_target_weight",
    "live_target_weight",
    "target_weight_delta_live_minus_rest",
    "rest_decision",
    "live_shadow_decision",
    "decision_agreement",
    "live_preference_direction",
    "start_live_price",
    "outcome_live_price",
    "forward_return_10m",
    "forward_return_30m",
    "forward_return_60m",
    "live_preference_score_10m",
    "live_preference_score_30m",
    "live_preference_score_60m",
    "live_preference_result_10m",
    "live_preference_result_30m",
    "live_preference_result_60m",
    "finalized_reason",
]


LAYER_STRATEGY_SHADOW_ORDER_FIELDS = [
    "timestamp",
    "cycle_id",
    "strategy_name",
    "source",
    "symbol",
    "side",
    "status",
    "skip_reason",
    "candidate_rank",
    "qty",
    "price",
    "notional",
    "requested_qty",
    "requested_notional",
    "max_trade_notional",
    "capped_qty",
    "current_qty_before",
    "target_qty",
    "qty_delta_before",
    "current_weight_before",
    "target_weight",
    "cash_before",
    "cash_after",
    "equity_before",
    "equity_after",
    "planner_source",
    "planner_decision",
    "planner_target_seen_count",
    "planner_target_absent_count",
    "reason",
]


LAYER_STRATEGY_SHADOW_PORTFOLIO_FIELDS = [
    "timestamp",
    "cycle_id",
    "strategy_name",
    "source",
    "symbol",
    "qty",
    "price",
    "market_value",
    "weight",
    "target_weight",
    "weight_drift",
    "cash",
    "equity",
    "cash_pct",
    "target_cash_pct",
    "trade_count",
    "buy_notional",
    "sell_notional",
    "gross_turnover",
    "cumulative_trade_count",
    "cumulative_buy_notional",
    "cumulative_sell_notional",
    "cumulative_gross_turnover",
    "peak_equity",
    "drawdown_pct",
    "target_summary",
]


LAYER_STRATEGY_SHADOW_COMPARISON_FIELDS = [
    "timestamp",
    "cycle_id",
    "status",
    "market_is_open",
    "rest_status",
    "live_status",
    "rest_equity",
    "live_equity",
    "live_minus_rest_equity",
    "rest_cash_pct",
    "live_cash_pct",
    "rest_cycle_gross_turnover",
    "live_cycle_gross_turnover",
    "rest_cumulative_gross_turnover",
    "live_cumulative_gross_turnover",
    "rest_drawdown_pct",
    "live_drawdown_pct",
    "rest_planner_status",
    "live_planner_status",
    "rest_planner_decision_counts",
    "live_planner_decision_counts",
    "winner_by_equity",
    "live_better_than_rest",
    "rest_top_weights",
    "live_top_weights",
    "error",
]


LAYER_OPENING_SHADOW_CYCLE_FIELDS = [
    "timestamp",
    "cycle_id",
    "market_is_open",
    "rest_status",
    "rest_fresh_count",
    "required_fresh_symbols",
    "live_status",
    "live_ranked_count",
    "live_symbols_ready_count",
    "symbol_count",
    "warmup_available",
    "warmup_age_minutes",
    "warmup_target_symbols",
    "live_top_symbols",
    "live_cash_pct",
    "warmup_cash_pct",
    "total_abs_live_vs_warmup_target_diff",
    "live_only_buy_count",
    "live_only_sell_count",
    "live_only_hold_count",
    "hybrid_execute_count",
    "hybrid_delay_count",
    "hybrid_block_count",
    "hybrid_buy_count",
    "hybrid_sell_count",
    "error",
]


LAYER_REST_LIVE_ATTRIBUTION_CYCLE_FIELDS = [
    "timestamp",
    "cycle_id",
    "market_is_open",
    "rest_status",
    "live_status",
    "rest_equity",
    "live_equity",
    "live_minus_rest",
    "rest_cash_target",
    "live_cash_target",
    "cash_target_delta_live_minus_rest",
    "rest_cash_actual",
    "live_cash_actual",
    "target_diff_total",
    "decision_agreement_rate",
    "top5_overlap_count",
    "top5_overlap_symbols",
    "disagreement_count",
    "top_disagreement_symbols",
    "avg_abs_score_diff",
    "attribution_horizon_minutes",
    "biggest_rest_advantage_symbol",
    "biggest_rest_advantage_estimated_pl",
    "biggest_live_advantage_symbol",
    "biggest_live_advantage_estimated_pl",
    "attributed_estimated_pl_total",
    "symbol_count",
    "symbol_outcome_count",
    "error",
]


LAYER_REST_LIVE_ATTRIBUTION_SYMBOL_FIELDS = [
    "timestamp",
    "outcome_timestamp",
    "cycle_id",
    "symbol",
    "market_is_open",
    "rest_status",
    "live_status",
    "rest_rank",
    "live_rank",
    "rank_delta_live_minus_rest",
    "rest_score",
    "live_score",
    "score_delta_live_minus_rest",
    "rest_target_weight",
    "live_target_weight",
    "target_weight_delta_live_minus_rest",
    "current_weight",
    "rest_effective_shadow_weight",
    "live_effective_shadow_weight",
    "effective_shadow_weight_delta_live_minus_rest",
    "reference_equity",
    "rest_decision",
    "live_implied_decision",
    "decision_agreement",
    "start_price",
    "outcome_price",
    "forward_return_10m",
    "forward_return_30m",
    "forward_return_60m",
    "estimated_pl_diff_10m",
    "estimated_pl_diff_30m",
    "estimated_pl_diff_60m",
    "better_source_10m",
    "better_source_30m",
    "better_source_60m",
    "better_source",
    "estimate_basis",
    "rest_reason",
    "live_reason",
    "finalized_reason",
    "error",
]


LAYER_OPENING_SHADOW_TRADE_FIELDS = [
    "timestamp",
    "cycle_id",
    "symbol",
    "market_is_open",
    "rest_status",
    "rest_fresh_count",
    "required_fresh_symbols",
    "live_status",
    "live_bar_count",
    "current_qty",
    "current_weight",
    "live_target_weight",
    "warmup_target_weight",
    "target_delta_live_minus_current",
    "target_delta_warmup_minus_current",
    "live_only_decision",
    "live_only_reason",
    "live_only_qty",
    "live_only_notional",
    "live_only_agrees_with_warmup",
    "hybrid_decision",
    "hybrid_action",
    "hybrid_reason",
    "hybrid_qty",
    "hybrid_notional",
    "live_price",
    "live_rank",
    "live_score",
    "warmup_rank",
    "warmup_score",
    "live_top_symbols",
    "warmup_target_symbols",
]


LAYER_OPENING_SHADOW_OUTCOME_FIELDS = [
    "source_timestamp",
    "outcome_timestamp",
    "source_cycle_id",
    "symbol",
    "strategy_name",
    "proposed_decision",
    "proposed_action",
    "start_live_price",
    "outcome_live_price",
    "forward_return_10m",
    "forward_return_30m",
    "forward_return_60m",
    "trade_score_10m",
    "trade_score_30m",
    "trade_score_60m",
    "trade_result_10m",
    "trade_result_30m",
    "trade_result_60m",
    "current_weight",
    "live_target_weight",
    "warmup_target_weight",
    "proposed_qty",
    "proposed_notional",
    "reason",
    "finalized_reason",
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


def _rewrite_csv_header_if_needed(path: Path, fieldnames: list[str]) -> bool:
    """
    Return True if caller should write a new header.

    If the file already exists but its header is stale, rewrite it with the
    current fieldnames while preserving existing rows. This keeps CSV schema
    changes, such as new Layer cycle diagnostics, readable by DictReader.
    """
    if not path.exists() or path.stat().st_size == 0:
        return True

    try:
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_fieldnames = reader.fieldnames or []

            if existing_fieldnames == fieldnames:
                return False

            existing_rows = list(reader)

        tmp_path = path.with_name(f"{path.name}.tmp")

        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )
            writer.writeheader()

            for row in existing_rows:
                writer.writerow({
                    key: _csv_value(row.get(key))
                    for key in fieldnames
                })

        tmp_path.replace(path)

        logging.info(
            "[LayerCSV] Rewrote CSV header for schema update | file=%s old_fields=%s new_fields=%s",
            path.name,
            existing_fieldnames,
            fieldnames,
        )

        return False

    except Exception:
        logging.warning(
            "[LayerCSV] Failed checking/rebuilding CSV header for %s. Appending with existing behavior.",
            path,
            exc_info=True,
        )
        return False


def _append_csv_rows(filename: str, fieldnames: list[str], rows: list[dict]) -> None:
    if not rows:
        return

    path = layer_csv_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)

    with _LAYER_CSV_LOCK:
        write_header = _rewrite_csv_header_if_needed(path, fieldnames)

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


def _replace_csv_rows(
    filename: str,
    fieldnames: list[str],
    rows: list[dict],
) -> None:
    """
    Atomically replace one derived CSV with the provided rows.

    The attribution CSVs are derived views, so rebuilding them prevents
    duplicate source/outcome rows and fills forward returns in place.
    """
    path = layer_csv_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(path.suffix + ".tmp")

    with _LAYER_CSV_LOCK:
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )
            writer.writeheader()

            for row in rows or []:
                writer.writerow({
                    key: _csv_value(row.get(key))
                    for key in fieldnames
                })

        tmp_path.replace(path)


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
            "planner_source": (
                row.get("planner_source")
                or summary.get("planner_source")
            ),
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
            "submitted_side": order.get("submitted_side"),
            "order_request_side": order.get("order_request_side"),
            "order_type": order.get("order_type"),
            "time_in_force": order.get("time_in_force"),
            "market_is_open": order.get("market_is_open"),
            "broker_error_code": order.get("broker_error_code"),
            "broker_error_message": order.get("broker_error_message"),
            "broker_error_existing_qty": order.get("broker_error_existing_qty"),
            "broker_error_available_qty": order.get("broker_error_available_qty"),
            "broker_error_held_for_orders": order.get("broker_error_held_for_orders"),
            "broker_error_symbol": order.get("broker_error_symbol"),
            "broker_error_raw": order.get("broker_error_raw"),
            "cooldown_until": order.get("cooldown_until"),
            "cooldown_remaining_seconds": order.get("cooldown_remaining_seconds"),
            "position_qty_before": order.get("position_qty_before"),
            "open_order_count_for_symbol": order.get("open_order_count_for_symbol"),
            "cash_budget_before": order.get("cash_budget_before"),
            "cash_budget_after": order.get("cash_budget_after"),
        })

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["orders"],
            LAYER4_ORDER_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append Layer 4 order rows.", exc_info=True)


def append_layer4_shadow_rows(result: dict | None) -> None:
    result = result or {}
    shadow_rows = result.get("rows", []) or []

    rows = []

    for shadow in shadow_rows:
        if not isinstance(shadow, dict):
            continue

        rows.append({
            "timestamp": shadow.get("timestamp") or datetime.utcnow().isoformat(),
            "finished_at": result.get("finished_at"),
            "cycle_id": shadow.get("cycle_id") or result.get("cycle_id"),
            "plan_id": shadow.get("plan_id") or result.get("plan_id"),
            "row_id": shadow.get("row_id"),
            "symbol": shadow.get("symbol"),
            "side": shadow.get("side"),
            "decision": shadow.get("decision"),
            "layer3_reason": shadow.get("layer3_reason"),
            "shadow_action": shadow.get("shadow_action"),
            "shadow_confidence": shadow.get("shadow_confidence"),
            "shadow_reason": shadow.get("shadow_reason"),
            "would_execute": shadow.get("would_execute"),
            "would_delay": shadow.get("would_delay"),
            "would_reduce": shadow.get("would_reduce"),
            "would_block": shadow.get("would_block"),
            "recommended_qty_multiplier": shadow.get("recommended_qty_multiplier"),
            "live_pressure_score": shadow.get("live_pressure_score"),
            "buy_chase_risk": shadow.get("buy_chase_risk"),
            "sell_strength_protection": shadow.get("sell_strength_protection"),
            "sell_classification": shadow.get("sell_classification"),
            "position_green": shadow.get("position_green"),
            "position_unrealized_plpc": shadow.get("position_unrealized_plpc"),
            "live_tick_count": shadow.get("live_tick_count"),
            "live_1m_bar_count": shadow.get("live_1m_bar_count"),
            "live_5m_bar_count": shadow.get("live_5m_bar_count"),
            "live_price": shadow.get("live_price"),
            "row_price": shadow.get("row_price"),
            "live_vs_row_price_pct": shadow.get("live_vs_row_price_pct"),
            "latest_1m_close": shadow.get("latest_1m_close"),
            "latest_5m_close": shadow.get("latest_5m_close"),
            "latest_1m_volume": shadow.get("latest_1m_volume"),
            "latest_5m_volume": shadow.get("latest_5m_volume"),
            "live_ret_30s": shadow.get("live_ret_30s"),
            "live_ret_60s": shadow.get("live_ret_60s"),
            "live_ret_300s": shadow.get("live_ret_300s"),
            "live_range_component": shadow.get("live_range_component"),
            "live_volume_component": shadow.get("live_volume_component"),
            "qty": shadow.get("qty"),
            "notional": shadow.get("notional"),
            "price": shadow.get("price"),
            "current_qty": shadow.get("current_qty"),
            "target_qty": shadow.get("target_qty"),
            "current_weight": shadow.get("current_weight"),
            "target_weight": shadow.get("target_weight"),
            "delta_weight": shadow.get("delta_weight"),
            "relative_drift": shadow.get("relative_drift"),
            "shadow_row_count": result.get("row_count"),
            "shadow_execute_count": result.get("execute_count"),
            "shadow_delay_count": result.get("delay_count"),
            "shadow_reduce_count": result.get("reduce_count"),
            "shadow_block_count": result.get("block_count"),
            "shadow_error": result.get("error"),
            "duration_seconds": result.get("duration_seconds"),
        })

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["shadow"],
            LAYER4_SHADOW_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append Layer 4 shadow rows.", exc_info=True)


def append_layer_live_bar_health_rows(rows: list[dict] | None) -> None:
    rows = rows or []

    if not rows:
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["live-bar-health"],
            LAYER_LIVE_BAR_HEALTH_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append live bar health rows.", exc_info=True)


def append_layer_live_strategy_shadow_rows(rows: list[dict] | None) -> None:
    rows = rows or []

    if not rows:
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["live-strategy-shadow"],
            LAYER_LIVE_STRATEGY_SHADOW_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append live strategy shadow rows.", exc_info=True)


def append_layer_live_strategy_shadow_cycle_row(row: dict | None) -> None:
    if not isinstance(row, dict) or not row:
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["live-strategy-shadow-cycles"],
            LAYER_LIVE_STRATEGY_SHADOW_CYCLE_FIELDS,
            [row],
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append live strategy shadow cycle row.", exc_info=True)


def append_layer_live_strategy_outcome_rows(rows: list[dict] | None) -> None:
    rows = rows or []

    if not rows:
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["live-strategy-outcomes"],
            LAYER_LIVE_STRATEGY_OUTCOME_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append live strategy outcome rows.", exc_info=True)


def append_layer_strategy_shadow_order_rows(rows: list[dict] | None) -> None:
    rows = rows or []

    if not rows:
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["strategy-shadow-orders"],
            LAYER_STRATEGY_SHADOW_ORDER_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append strategy shadow order rows.", exc_info=True)


def append_layer_strategy_shadow_portfolio_rows(rows: list[dict] | None) -> None:
    rows = rows or []

    if not rows:
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["strategy-shadow-portfolios"],
            LAYER_STRATEGY_SHADOW_PORTFOLIO_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append strategy shadow portfolio rows.", exc_info=True)


def append_layer_strategy_shadow_comparison_row(row: dict | None) -> None:
    if not isinstance(row, dict) or not row:
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["strategy-shadow-comparison"],
            LAYER_STRATEGY_SHADOW_COMPARISON_FIELDS,
            [row],
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append strategy shadow comparison row.", exc_info=True)


def replace_layer_rest_live_attribution_cycle_rows(
    rows: list[dict] | None,
) -> None:
    try:
        _replace_csv_rows(
            LAYER_CSV_FILES["rest-live-attribution-cycles"],
            LAYER_REST_LIVE_ATTRIBUTION_CYCLE_FIELDS,
            rows or [],
        )
    except Exception:
        logging.warning(
            "[LayerCSV] Failed to rebuild REST-vs-LIVE attribution cycle CSV.",
            exc_info=True,
        )


def replace_layer_rest_live_attribution_symbol_rows(
    rows: list[dict] | None,
) -> None:
    try:
        _replace_csv_rows(
            LAYER_CSV_FILES["rest-live-attribution-symbols"],
            LAYER_REST_LIVE_ATTRIBUTION_SYMBOL_FIELDS,
            rows or [],
        )
    except Exception:
        logging.warning(
            "[LayerCSV] Failed to rebuild REST-vs-LIVE attribution symbol CSV.",
            exc_info=True,
        )


def append_layer_opening_shadow_cycle_row(row: dict | None) -> None:
    if not isinstance(row, dict) or not row:
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["opening-shadow-cycles"],
            LAYER_OPENING_SHADOW_CYCLE_FIELDS,
            [row],
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append opening shadow cycle row.", exc_info=True)


def append_layer_opening_shadow_trade_rows(rows: list[dict] | None) -> None:
    rows = rows or []

    if not rows:
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["opening-shadow-trades"],
            LAYER_OPENING_SHADOW_TRADE_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append opening shadow trade rows.", exc_info=True)


def append_layer_opening_shadow_outcome_rows(rows: list[dict] | None) -> None:
    rows = rows or []

    if not rows:
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES["opening-shadow-outcomes"],
            LAYER_OPENING_SHADOW_OUTCOME_FIELDS,
            rows,
        )
    except Exception:
        logging.warning("[LayerCSV] Failed to append opening shadow outcome rows.", exc_info=True)


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
    layer4_shadow_result = layer4_result.get("shadow_result") or {}

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

        "account_source": layer3_summary.get("account_source"),
        "broker_snapshot_ok": layer3_summary.get("broker_snapshot_ok"),
        "account_snapshot_error": layer3_summary.get("account_snapshot_error"),

        "confirmation_updates_allowed": layer3_summary.get("confirmation_updates_allowed"),
        "confirmation_updates_blocked_reason": layer3_summary.get("confirmation_updates_blocked_reason"),

        "open_session_date": layer3_summary.get("open_session_date"),
        "open_session_live_cycle_count": layer3_summary.get("open_session_live_cycle_count"),
        "open_session_reset_seen_symbols": layer3_summary.get("open_session_reset_seen_symbols"),
        "open_session_reset_absent_symbols": layer3_summary.get("open_session_reset_absent_symbols"),

        "opening_transition_active": layer3_summary.get("opening_transition_active"),
        "opening_transition_cycles": layer3_summary.get("opening_transition_cycles"),

        "bootstrap_confirmation_applied": layer3_summary.get("bootstrap_confirmation_applied"),
        "bootstrap_confirmation_symbols": layer3_summary.get("bootstrap_confirmation_symbols"),
        "bootstrap_confirmation_warmup_filter_applied": layer3_summary.get("bootstrap_confirmation_warmup_filter_applied"),
        "bootstrap_confirmation_warmup_symbols": layer3_summary.get("bootstrap_confirmation_warmup_symbols"),
        "bootstrap_confirmation_warmup_target_symbols": layer3_summary.get("bootstrap_confirmation_warmup_target_symbols"),
        "bootstrap_confirmation_warmup_skipped_symbols": layer3_summary.get("bootstrap_confirmation_warmup_skipped_symbols"),
        "bootstrap_confirmation_warmup_stale_symbols": layer3_summary.get("bootstrap_confirmation_warmup_stale_symbols"),
        "bootstrap_confirmation_warmup_missing_age_symbols": layer3_summary.get("bootstrap_confirmation_warmup_missing_age_symbols"),
        "bootstrap_confirmation_warmup_freshness_available": layer3_summary.get("bootstrap_confirmation_warmup_freshness_available"),
        "bootstrap_confirmation_warmup_max_age_minutes": layer3_summary.get("bootstrap_confirmation_warmup_max_age_minutes"),

        "layer4_attempted": layer4_result.get("attempted"),
        "layer4_submitted": layer4_result.get("submitted"),
        "layer4_skipped": layer4_result.get("skipped"),
        "layer4_errors": layer4_result.get("errors"),
        "layer4_blocked_reason": layer4_result.get("blocked_reason"),
        "layer4_count_integrity_ok": layer4_result.get("count_integrity_ok"),

        "layer4_shadow_rows": layer4_shadow_result.get("row_count"),
        "layer4_shadow_execute": layer4_shadow_result.get("execute_count"),
        "layer4_shadow_delay": layer4_shadow_result.get("delay_count"),
        "layer4_shadow_reduce": layer4_shadow_result.get("reduce_count"),
        "layer4_shadow_block": layer4_shadow_result.get("block_count"),

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