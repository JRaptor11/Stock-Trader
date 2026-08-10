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
_SESSION_EQUITY_PEAKS: dict[str, dict] = {}


LAYER_CSV_FILES = {
    "cycles": "layer_cycles.csv",
    "plans": "layer3_plans.csv",
    "orders": "layer4_orders.csv",
    "order-outcomes": (
        "layer_order_outcomes.csv"
    ),
    "order-outcome-cycles": (
        "layer_order_outcome_cycles.csv"
    ),
    "shadow": "layer4_shadow.csv",
    "shadow-lifecycle": "layer4_shadow_lifecycle.csv",
    "research-strategy-cycles": "layer_research_strategy_cycles.csv",
    "research-strategy-decisions": "layer_research_strategy_decisions.csv",
    "research-strategy-orders": "layer_research_strategy_orders.csv",
    "research-strategy-portfolios": "layer_research_strategy_portfolios.csv",
    "portfolio-snapshots": "layer_portfolio_snapshots.csv",
    "fail-safe-lifecycle": "fail_safe_lifecycle.csv",
    "fail-safe-position-observations": (
        "fail_safe_position_observations.csv"
    ),
    "daily-account-snapshots": "daily_account_snapshots.csv",
    "daily-position-snapshots": "daily_position_snapshots.csv",
    "daily-benchmark-snapshots": "daily_benchmark_snapshots.csv",
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

    "rolling_window_seconds",
    "rolling_trades_used_before",
    "rolling_buys_used_before",
    "rolling_sells_used_before",
    "rolling_buy_notional_used_before",
    "rolling_sell_notional_used_before",
    "rolling_gross_notional_used_before",

    "rolling_trade_limit",
    "rolling_buy_limit",
    "rolling_sell_limit",
    "rolling_buy_notional_limit",
    "rolling_sell_notional_limit",
    "rolling_gross_notional_limit",

    "rolling_trades_authorized",
    "rolling_buys_authorized",
    "rolling_sells_authorized",
    "rolling_buy_notional_authorized",
    "rolling_sell_notional_authorized",
    "rolling_gross_notional_authorized",

    "rolling_limit_adjusted_count",
    "rolling_limit_deferred_count",

    "rest_bar_gate_status",
    "rest_bar_candidate_timestamp",
    "rest_bar_wait_attempts",
    "rest_bar_wait_seconds",
    "rest_bar_new_symbol_count",
    "rest_bar_required_new_symbols",
    "rest_bar_new_symbols",
    "rest_bar_duplicate_symbols",
    "rest_bar_lagging_symbols",
    "rest_bar_missing_symbols",

    "live_bar_health_symbol_count",
    "live_bar_exact_match_count",
    "live_bar_comparison_eligible_count",
    "live_bar_full_capture_candidate_count",

    "live_bar_exact_match_rate",
    "live_bar_comparison_eligible_rate",
    "live_bar_full_capture_rate",

    "live_bar_match_status_counts",
    "live_bar_capture_quality_counts",
    "live_bar_unmatched_symbols",
    "live_bar_non_full_capture_symbols",

    "live_bar_median_abs_open_pct_delta",
    "live_bar_median_abs_high_pct_delta",
    "live_bar_median_abs_low_pct_delta",
    "live_bar_median_abs_close_pct_delta",
    "live_bar_max_abs_close_pct_delta",

    "live_bar_median_volume_capture_ratio",
    "live_bar_median_trade_count_capture_ratio",

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

    "source_bar_timestamp",
    "source_bar_is_new",
    "target_hysteresis_enabled",
    "target_hysteresis_action_counts",
    "target_hysteresis_pending_symbols",
    "target_hysteresis_changed_symbols",
    "target_hysteresis_raw_symbol_count",
    "target_hysteresis_approved_symbol_count",
    "target_hysteresis_approved_cash_pct",

    "open_session_date",
    "open_session_live_cycle_count",
    "open_session_reset_seen_symbols",
    "open_session_reset_absent_symbols",

    "opening_transition_active",
    "opening_transition_cycle",
    "opening_transition_cycles",
    "opening_transition_phase",
    "opening_transition_source_market_time",

    "restart_recovery_required",
    "restart_recovery_active",
    "restart_recovery_execution_blocked",
    "restart_recovery_reason",
    "restart_recovery_warmup_present",
    "restart_recovery_warmup_reason",
    "restart_recovery_warmup_trusted",
    "restart_recovery_warmup_timestamp",
    "restart_recovery_warmup_age_minutes",
    "restart_recovery_warmup_snapshot_fallback",
    "restart_recovery_observed_bars",
    "restart_recovery_required_bars",
    "restart_recovery_source_timestamps",
    "restart_recovery_ready_after_source_bar",
    "restart_recovery_completed",
    "restart_recovery_evidence_committed",
    "restart_recovery_baseline_seeded",
    "restart_recovery_baseline_symbols",
    "restart_recovery_baseline_seeded_this_cycle",
    "strategy_execution_blocked_reason",

    "bootstrap_confirmation_applied",
    "bootstrap_confirmation_blocked_reason",
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
    "session_peak_equity",
    "session_peak_timestamp",
    "equity_giveback_from_peak",
    "equity_giveback_from_peak_pct",
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

    "raw_target_weight",
    "approved_target_weight",
    "previous_approved_target_weight",
    "pending_target_weight",
    "target_candidate_direction",
    "target_candidate_count",
    "target_required_count",
    "target_confirmation_advanced",
    "target_confirmation_bar_timestamp",
    "target_confirmation_bar_is_new",
    "target_hysteresis_action",
    "target_hysteresis_reset_reason",
    "target_hysteresis_changed_target",
    "deferred_target_weight",
    "deferred_notional",

    "delta_weight",
    "relative_drift",
    "shadow_effective_qty",
    "shadow_effective_notional",
    "shadow_deferred_qty",
    "shadow_deferred_notional",
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

    "rolling_window_seconds",
    "rolling_trades_used_before",
    "rolling_buys_used_before",
    "rolling_sells_used_before",
    "rolling_buy_notional_used_before",
    "rolling_sell_notional_used_before",
    "rolling_gross_notional_used_before",

    "rolling_trade_limit",
    "rolling_buy_limit",
    "rolling_sell_limit",
    "rolling_buy_notional_limit",
    "rolling_sell_notional_limit",
    "rolling_gross_notional_limit",

    "rolling_limit_original_planned_notional",
    "rolling_limit_adjusted",
    "rolling_limit_blocked_reason",

    "target_seen_count",
    "target_absent_count",

    "bootstrap_confirmation_applied",
    "bootstrap_confirmation_symbols",
    "bootstrap_confirmation_warmup_filter_applied",
    "bootstrap_confirmation_warmup_skipped_symbols",
    "open_session_warmup_symbols",
    "open_session_reset_seen_symbols",
    "open_session_reset_absent_symbols",

    "opening_transition_active",
    "opening_transition_cycle",
    "opening_transition_cycles",
    "opening_transition_phase",
    "opening_transition_source_market_time",

    "restart_recovery_required",
    "restart_recovery_active",
    "restart_recovery_execution_blocked",
    "restart_recovery_reason",
    "restart_recovery_observed_bars",
    "restart_recovery_required_bars",
    "restart_recovery_source_timestamps",
    "restart_recovery_ready_after_source_bar",
    "restart_recovery_completed",
    "restart_recovery_evidence_committed",
    "restart_recovery_baseline_seeded",
    "restart_recovery_baseline_symbols",
    "restart_recovery_baseline_seeded_this_cycle",
    "strategy_execution_blocked_reason",
    "bootstrap_confirmation_blocked_reason",

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
    "execution_phase",
    "trade_attribution",
    "trade_attribution_evidence",
    "trade_attribution_detail",
    "extended_hours_fail_safe",
    "extended_hours_context",
    "status",
    "qty",
    "notional",
    "price",

    "submission_context_captured_at",
    "submission_plan_price",
    "submission_reference_price",
    "submission_reference_source",
    "submission_reference_tick_timestamp",
    "submission_reference_age_seconds",
    "submission_reference_vs_plan_pct",

    "broker_submit_started_at",
    "broker_submit_completed_at",
    "broker_submit_latency_ms",
    "broker_status_at_submit",
    "broker_created_at",
    "broker_submitted_at",
    "broker_limit_price",

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


LAYER_ORDER_OUTCOME_FIELDS = [
    "timestamp",

    "cycle_id",
    "plan_id",
    "row_id",

    "order_id",
    "client_order_id",
    "symbol",
    "side",

    "terminal_status",
    "terminal_observed_at",
    "terminal_at",

    "broker_submitted_at",
    "broker_updated_at",
    "broker_filled_at",
    "broker_canceled_at",
    "broker_expired_at",
    "broker_failed_at",

    "requested_qty",
    "filled_qty",
    "unfilled_qty",
    "fill_ratio",
    "filled_avg_price",
    "filled_notional",

    "submission_plan_price",
    "submission_reference_price",
    "submission_reference_source",
    "submission_reference_tick_timestamp",
    "submission_reference_age_seconds",
    "submission_reference_vs_plan_pct",

    "fill_vs_plan_pct",
    "fill_vs_reference_pct",
    "adverse_slippage_vs_plan_pct",
    "adverse_slippage_vs_reference_pct",

    "submission_to_terminal_seconds",
    "time_to_fill_seconds",
    "monitor_detection_delay_seconds",

    "broker_submit_started_at",
    "broker_submit_completed_at",
    "broker_submit_latency_ms",
    "broker_status_at_submit",
    "broker_created_at",
    "broker_limit_price",

    "tracked_at",
    "market_is_open",
    "reason",
    "planned_notional",

    "cancel_requested",
    "cancel_reason",
    "cancel_requested_at",

    "terminal_message",
]


LAYER_ORDER_OUTCOME_CYCLE_FIELDS = [
    "timestamp",

    "cycle_id",
    "plan_id",

    "execution_started_at",
    "execution_finished_at",

    "execution_reported_submitted_count",
    "expected_submitted_count",
    "terminal_order_count",
    "cycle_complete",
    "submitted_count_integrity_ok",

    "expected_buy_count",
    "expected_sell_count",

    "filled_order_count",
    "nonfilled_terminal_count",
    "filled_buy_count",
    "filled_sell_count",
    "full_fill_count",

    "fill_rate",
    "full_fill_rate",

    "terminal_status_counts",

    "requested_qty_total",
    "filled_qty_total",
    "filled_notional_total",

    "reference_slippage_sample_count",
    "reference_slippage_coverage_rate",
    "plan_slippage_sample_count",
    "plan_slippage_coverage_rate",

    "median_time_to_fill_seconds",
    "max_time_to_fill_seconds",

    "median_monitor_detection_delay_seconds",
    "max_monitor_detection_delay_seconds",

    "median_adverse_slippage_vs_reference_pct",
    "max_adverse_slippage_vs_reference_pct",

    "median_adverse_slippage_vs_plan_pct",
    "max_adverse_slippage_vs_plan_pct",

    "median_buy_adverse_slippage_vs_reference_pct",
    "median_sell_adverse_slippage_vs_reference_pct",

    "total_adverse_slippage_vs_reference_dollars",
    "total_adverse_slippage_vs_plan_dollars",

    "worst_reference_order_id",
    "worst_reference_symbol",
    "worst_reference_side",
    "worst_reference_adverse_slippage_pct",
    "worst_reference_adverse_slippage_dollars",

    "terminal_symbols",
    "filled_symbols",
    "nonfilled_symbols",
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


LAYER_REST_LIVE_BAR_COMPARISON_FIELDS = [
    "bar_match_status",
    "bar_match_exact",
    "bar_match_comparison_eligible",

    "bar_match_rest_timestamp",
    "bar_match_rest_end_timestamp",

    "bar_match_live_timestamp",
    "bar_match_live_end_timestamp",

    "bar_match_nearest_live_timestamp",
    "bar_match_nearest_live_delta_seconds",

    "bar_match_live_sealed",
    "bar_match_live_sealed_at",
    "bar_match_live_capture_quality",
    "bar_match_live_full_capture_candidate",
    "bar_match_live_late_created_after_seal",

    "bar_match_live_event_timestamp_fallback_count",
    "bar_match_live_trade_id_missing_count",
    "bar_match_live_duplicate_trade_message_count",
    "bar_match_live_late_after_seal_trade_count",
    "bar_match_live_late_after_seal_volume",

    "rest_open",
    "rest_high",
    "rest_low",
    "rest_close",
    "rest_volume",
    "rest_trade_count",
    "rest_vwap",

    "matched_live_open",
    "matched_live_high",
    "matched_live_low",
    "matched_live_close",
    "matched_live_volume",
    "matched_live_trade_count",
    "matched_live_vwap",

    "open_delta_live_minus_rest",
    "high_delta_live_minus_rest",
    "low_delta_live_minus_rest",
    "close_delta_live_minus_rest",
    "volume_delta_live_minus_rest",
    "trade_count_delta_live_minus_rest",
    "vwap_delta_live_minus_rest",

    "open_pct_delta_live_minus_rest",
    "high_pct_delta_live_minus_rest",
    "low_pct_delta_live_minus_rest",
    "close_pct_delta_live_minus_rest",
    "volume_pct_delta_live_minus_rest",
    "trade_count_pct_delta_live_minus_rest",
    "vwap_pct_delta_live_minus_rest",

    "volume_capture_ratio_live_to_rest",
    "trade_count_capture_ratio_live_to_rest",
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

    # Legacy newest-tick versus delayed REST-close metric.
    "live_vs_rest_close_pct",

    # Exact five-minute REST/LIVE bucket diagnostics.
    *LAYER_REST_LIVE_BAR_COMPARISON_FIELDS,
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

    "live_planner_raw_target_weight",
    "live_planner_approved_target_weight",
    "live_planner_pending_target_weight",
    "live_planner_target_candidate_direction",
    "live_planner_target_candidate_count",
    "live_planner_target_required_count",
    "live_planner_target_hysteresis_action",
    "live_planner_deferred_notional",

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
    "live_cohort_status",
    "live_cohort_timestamp",
    "live_cohort_symbol_count",
    "live_cohort_symbols",
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
    "live_planner_rolling_trade_limits",
    "live_planner_target_hysteresis",
    "rest_ranker_code_hash",
    "live_ranker_code_hash",
    "ranker_code_match",
    "rest_layer2_config_hash",
    "live_layer2_config_hash",
    "layer2_config_match",
    "layer3_code_hash",
    "layer3_config_hash",
    "simulator_config_hash",
    "strategy_parity_verified",
    "error",
]


LAYER4_SHADOW_LIFECYCLE_FIELDS = [
    "timestamp", "cycle_id", "plan_id", "row_id", "symbol", "side",
    "event", "status", "original_cycle_id", "original_plan_id",
    "original_row_id", "original_action", "original_qty", "original_price",
    "current_action", "current_qty", "current_price", "age_cycles",
    "price_change_pct", "estimated_entry_improvement", "resolution_reason",
]


LAYER_RESEARCH_STRATEGY_CYCLE_FIELDS = [
    "timestamp", "cycle_id", "strategy_name", "status", "config_hash",
    "source_bar_timestamp", "production_equity", "shadow_equity",
    "shadow_minus_production_equity", "shadow_minus_control_equity",
    "shadow_pnl_since_initialization",
    "cash", "cash_pct", "peak_equity", "drawdown_pct", "peak_giveback",
    "invested_pct", "target_mode", "signal_horizon_minutes",
    "reversal_detected_count", "reversal_sell_count",
    "reversal_exposure", "reversal_exposure_pct",
    "cumulative_trade_count", "cumulative_gross_turnover", "gross_turnover_pct",
    "follow_up_trade_count", "direction_reversal_count",
    "same_day_round_trip_symbol_count",
    "pnl_after_1bp_cost", "pnl_after_5bp_cost", "pnl_after_10bp_cost",
    "pnl_after_20bp_cost", "qualified_count", "selected_count",
    "rejected_count", "deteriorating_count", "target_cash_pct",
    "target_summary", "planner_status", "planner_decision_counts",
    "planner_rolling_trade_limits", "planner_target_hysteresis",
]

LAYER_RESEARCH_STRATEGY_DECISION_FIELDS = [
    "timestamp", "cycle_id", "strategy_name", "symbol", "production_rank",
    "base_score", "ret_30m", "ret_60m", "ret_150m", "ret_300m",
    "signal_horizon", "signal_score", "horizon_agreement_count",
    "reversal_detected",
    "momentum_acceleration", "volatility_300m", "positive_absolute_signal",
    "severe_deterioration", "moderate_deterioration",
    "qualification_multiplier", "effective_score", "qualification_reason",
    "qualified", "selected", "raw_target_weight", "smoothed_target_weight",
]

LAYER_RESEARCH_STRATEGY_ORDER_FIELDS = [
    "timestamp", "cycle_id", "strategy_name", "symbol", "side", "status",
    "qty", "price", "notional", "requested_qty", "cash_before", "cash_after",
    "position_qty_before", "position_qty_after", "reason", "is_follow_up",
]

LAYER_RESEARCH_STRATEGY_PORTFOLIO_FIELDS = [
    "timestamp", "cycle_id", "strategy_name", "symbol", "qty", "price",
    "market_value", "weight", "target_weight", "cash", "equity",
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
    "planner_raw_target_weight",
    "planner_approved_target_weight",
    "planner_pending_target_weight",
    "planner_target_candidate_direction",
    "planner_target_candidate_count",
    "planner_target_required_count",
    "planner_target_hysteresis_action",
    "planner_deferred_notional",
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
    "rest_planner_rolling_trade_limits",
    "live_planner_rolling_trade_limits",
    "rest_planner_target_hysteresis",
    "live_planner_target_hysteresis",
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
    "live_source_bar_timestamp",
    "live_source_bar_end_timestamp",
    "live_source_bar_symbol_count",
    "live_source_bar_symbols",
    "live_cohort_advanced",
    "duplicate_live_cohort",
    "live_ranked_count",
    "live_symbols_ready_count",
    "symbol_count",
    "warmup_present",
    "warmup_available",
    "warmup_reason",
    "warmup_trusted_for_restart_recovery",
    "warmup_timestamp",
    "warmup_age_minutes",
    "warmup_snapshot_fallback",
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
    "live_source_bar_timestamp",
    "live_source_bar_end_timestamp",
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

            "raw_target_weight": row.get(
                "raw_target_weight"
            ),
            "approved_target_weight": row.get(
                "approved_target_weight"
            ),
            "previous_approved_target_weight": row.get(
                "previous_approved_target_weight"
            ),
            "pending_target_weight": row.get(
                "pending_target_weight"
            ),
            "target_candidate_direction": row.get(
                "target_candidate_direction"
            ),
            "target_candidate_count": row.get(
                "target_candidate_count"
            ),
            "target_required_count": row.get(
                "target_required_count"
            ),
            "target_confirmation_advanced": row.get(
                "target_confirmation_advanced"
            ),
            "target_confirmation_bar_timestamp": row.get(
                "target_confirmation_bar_timestamp"
            ),
            "target_confirmation_bar_is_new": row.get(
                "target_confirmation_bar_is_new"
            ),
            "target_hysteresis_action": row.get(
                "target_hysteresis_action"
            ),
            "target_hysteresis_reset_reason": row.get(
                "target_hysteresis_reset_reason"
            ),
            "target_hysteresis_changed_target": row.get(
                "target_hysteresis_changed_target"
            ),
            "deferred_target_weight": row.get(
                "deferred_target_weight"
            ),
            "deferred_notional": row.get(
                "deferred_notional"
            ),

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

            "rolling_window_seconds": row.get(
                "rolling_window_seconds"
            ),
            "rolling_trades_used_before": row.get(
                "rolling_trades_used_before"
            ),
            "rolling_buys_used_before": row.get(
                "rolling_buys_used_before"
            ),
            "rolling_sells_used_before": row.get(
                "rolling_sells_used_before"
            ),
            "rolling_buy_notional_used_before": row.get(
                "rolling_buy_notional_used_before"
            ),
            "rolling_sell_notional_used_before": row.get(
                "rolling_sell_notional_used_before"
            ),
            "rolling_gross_notional_used_before": row.get(
                "rolling_gross_notional_used_before"
            ),
            "rolling_trade_limit": row.get(
                "rolling_trade_limit"
            ),
            "rolling_buy_limit": row.get(
                "rolling_buy_limit"
            ),
            "rolling_sell_limit": row.get(
                "rolling_sell_limit"
            ),
            "rolling_buy_notional_limit": row.get(
                "rolling_buy_notional_limit"
            ),
            "rolling_sell_notional_limit": row.get(
                "rolling_sell_notional_limit"
            ),
            "rolling_gross_notional_limit": row.get(
                "rolling_gross_notional_limit"
            ),
            "rolling_limit_original_planned_notional": row.get(
                "rolling_limit_original_planned_notional"
            ),
            "rolling_limit_adjusted": row.get(
                "rolling_limit_adjusted"
            ),
            "rolling_limit_blocked_reason": row.get(
                "rolling_limit_blocked_reason"
            ),

            "target_seen_count": row.get("target_seen_count"),
            "target_absent_count": row.get("target_absent_count"),

            "bootstrap_confirmation_applied": summary.get("bootstrap_confirmation_applied"),
            "bootstrap_confirmation_symbols": summary.get("bootstrap_confirmation_symbols"),
            "bootstrap_confirmation_warmup_filter_applied": summary.get("bootstrap_confirmation_warmup_filter_applied"),
            "bootstrap_confirmation_warmup_skipped_symbols": summary.get("bootstrap_confirmation_warmup_skipped_symbols"),
            "open_session_warmup_symbols": summary.get("open_session_warmup_symbols"),
            "open_session_reset_seen_symbols": summary.get("open_session_reset_seen_symbols"),
            "open_session_reset_absent_symbols": summary.get("open_session_reset_absent_symbols"),

            "opening_transition_active": summary.get("opening_transition_active"),
            "opening_transition_cycle": summary.get("opening_transition_cycle"),
            "opening_transition_cycles": summary.get("opening_transition_cycles"),
            "opening_transition_phase": summary.get("opening_transition_phase"),
            "opening_transition_source_market_time": summary.get("opening_transition_source_market_time"),

            "restart_recovery_required": summary.get("restart_recovery_required"),
            "restart_recovery_active": summary.get("restart_recovery_active"),
            "restart_recovery_execution_blocked": summary.get("restart_recovery_execution_blocked"),
            "restart_recovery_reason": summary.get("restart_recovery_reason"),
            "restart_recovery_observed_bars": summary.get("restart_recovery_observed_bars"),
            "restart_recovery_required_bars": summary.get("restart_recovery_required_bars"),
            "restart_recovery_source_timestamps": summary.get("restart_recovery_source_timestamps"),
            "restart_recovery_ready_after_source_bar": summary.get("restart_recovery_ready_after_source_bar"),
            "restart_recovery_completed": summary.get("restart_recovery_completed"),
            "restart_recovery_evidence_committed": summary.get("restart_recovery_evidence_committed"),
            "restart_recovery_baseline_seeded": summary.get("restart_recovery_baseline_seeded"),
            "restart_recovery_baseline_symbols": summary.get("restart_recovery_baseline_symbols"),
            "restart_recovery_baseline_seeded_this_cycle": summary.get("restart_recovery_baseline_seeded_this_cycle"),
            "strategy_execution_blocked_reason": summary.get("strategy_execution_blocked_reason"),
            "bootstrap_confirmation_blocked_reason": summary.get("bootstrap_confirmation_blocked_reason"),

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
            "execution_phase": order.get("execution_phase"),
            "trade_attribution": order.get("trade_attribution"),
            "trade_attribution_evidence": order.get(
                "trade_attribution_evidence"
            ),
            "trade_attribution_detail": order.get(
                "trade_attribution_detail"
            ),
            "extended_hours_fail_safe": order.get(
                "extended_hours_fail_safe"
            ),
            "extended_hours_context": order.get(
                "extended_hours_context"
            ),
            "qty": order.get("qty"),
            "notional": order.get("notional"),
            "price": order.get(
                "price"
            ),

            "submission_context_captured_at": (
                order.get(
                    "submission_context_captured_at"
                )
            ),
            "submission_plan_price": (
                order.get(
                    "submission_plan_price"
                )
            ),
            "submission_reference_price": (
                order.get(
                    "submission_reference_price"
                )
            ),
            "submission_reference_source": (
                order.get(
                    "submission_reference_source"
                )
            ),
            "submission_reference_tick_timestamp": (
                order.get(
                    "submission_reference_tick_timestamp"
                )
            ),
            "submission_reference_age_seconds": (
                order.get(
                    "submission_reference_age_seconds"
                )
            ),
            "submission_reference_vs_plan_pct": (
                order.get(
                    "submission_reference_vs_plan_pct"
                )
            ),

            "broker_submit_started_at": (
                order.get(
                    "broker_submit_started_at"
                )
            ),
            "broker_submit_completed_at": (
                order.get(
                    "broker_submit_completed_at"
                )
            ),
            "broker_submit_latency_ms": (
                order.get(
                    "broker_submit_latency_ms"
                )
            ),
            "broker_status_at_submit": (
                order.get(
                    "broker_status_at_submit"
                )
            ),
            "broker_created_at": (
                order.get(
                    "broker_created_at"
                )
            ),
            "broker_submitted_at": (
                order.get(
                    "broker_submitted_at"
                )
            ),
            "broker_limit_price": (
                order.get(
                    "broker_limit_price"
                )
            ),

            "order_id": order.get(
                "order_id"
            ),
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


def append_layer_order_outcome_cycle_row(
    row: dict | None,
) -> None:
    if (
        not isinstance(
            row,
            dict,
        )
        or not row
    ):
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES[
                "order-outcome-cycles"
            ],
            LAYER_ORDER_OUTCOME_CYCLE_FIELDS,
            [row],
        )

    except Exception:
        logging.warning(
            "[LayerCSV] Failed to append "
            "order-outcome cycle summary.",
            exc_info=True,
        )


def append_layer_order_outcome_row(
    row: dict | None,
) -> None:
    if (
        not isinstance(
            row,
            dict,
        )
        or not row
    ):
        return

    try:
        _append_csv_rows(
            LAYER_CSV_FILES[
                "order-outcomes"
            ],
            LAYER_ORDER_OUTCOME_FIELDS,
            [row],
        )

    except Exception:
        logging.warning(
            "[LayerCSV] Failed to append "
            "terminal order outcome row.",
            exc_info=True,
        )


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
            "shadow_effective_qty": shadow.get("shadow_effective_qty"),
            "shadow_effective_notional": shadow.get("shadow_effective_notional"),
            "shadow_deferred_qty": shadow.get("shadow_deferred_qty"),
            "shadow_deferred_notional": shadow.get("shadow_deferred_notional"),
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


def append_layer4_shadow_lifecycle_rows(rows: list[dict] | None) -> None:
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return
    try:
        _append_csv_rows(
            LAYER_CSV_FILES["shadow-lifecycle"],
            LAYER4_SHADOW_LIFECYCLE_FIELDS,
            rows,
        )
    except Exception:
        logging.warning(
            "[LayerCSV] Failed to append Layer 4 shadow lifecycle rows.",
            exc_info=True,
        )


def _append_research_rows(logical_name: str, fields: list[str], rows) -> None:
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return
    try:
        _append_csv_rows(LAYER_CSV_FILES[logical_name], fields, rows)
    except Exception:
        logging.warning("[LayerCSV] Failed appending %s rows.", logical_name, exc_info=True)


def append_layer_research_strategy_cycle_rows(rows) -> None:
    _append_research_rows("research-strategy-cycles", LAYER_RESEARCH_STRATEGY_CYCLE_FIELDS, rows)


def append_layer_research_strategy_decision_rows(rows) -> None:
    _append_research_rows("research-strategy-decisions", LAYER_RESEARCH_STRATEGY_DECISION_FIELDS, rows)


def append_layer_research_strategy_order_rows(rows) -> None:
    _append_research_rows("research-strategy-orders", LAYER_RESEARCH_STRATEGY_ORDER_FIELDS, rows)


def append_layer_research_strategy_portfolio_rows(rows) -> None:
    _append_research_rows("research-strategy-portfolios", LAYER_RESEARCH_STRATEGY_PORTFOLIO_FIELDS, rows)


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
    rest_bar_gate: dict | None = None,
    live_bar_summary: dict | None = None,
    ranked_count: int | None = None,
    top_symbols: list[str] | None = None,
    target_summary: dict | None = None,
    layer3_summary: dict | None = None,
    layer4_result: dict | None = None,
) -> None:
    target_summary = target_summary or {}
    layer3_summary = layer3_summary or {}
    layer4_result = layer4_result or {}
    rest_bar_gate = rest_bar_gate or {}
    live_bar_summary = (
        live_bar_summary
        or {}
    )

    layer4_shadow_result = (
        layer4_result.get(
            "shadow_result"
        )
        or {}
    )

    now_iso = datetime.utcnow().isoformat()
    session_key = str(layer3_summary.get("open_session_date") or now_iso[:10])
    equity_value = layer3_summary.get("equity")
    try:
        equity_number = float(equity_value)
    except (TypeError, ValueError):
        equity_number = 0.0
    peak_state = _SESSION_EQUITY_PEAKS.setdefault(
        session_key,
        {"equity": equity_number, "timestamp": now_iso},
    )
    if equity_number > float(peak_state.get("equity") or 0.0):
        peak_state.update({"equity": equity_number, "timestamp": now_iso})
    peak_equity = float(peak_state.get("equity") or 0.0)
    giveback = max(0.0, peak_equity - equity_number) if equity_number > 0 else None

    rolling_limits = (
        layer3_summary.get(
            "rolling_trade_limits"
        )
        or {}
    )

    rolling_usage_before = (
        rolling_limits.get("usage_before")
        or {}
    )

    rolling_limit_values = (
        rolling_limits.get("limits")
        or {}
    )

    rolling_authorized = (
        rolling_limits.get(
            "authorized_this_cycle"
        )
        or {}
    )

    target_hysteresis = (
        layer3_summary.get(
            "target_hysteresis"
        )
        or {}
    )

    row = {
        "timestamp": datetime.utcnow().isoformat(),
        "cycle_id": layer3_summary.get("cycle_id") or layer4_result.get("cycle_id"),
        "plan_id": layer3_summary.get("plan_id") or layer4_result.get("plan_id"),
        "status": status,
        "reason": reason,
        "market_is_open": market_is_open,
        "fresh_count": fresh_count,
        "required_fresh_symbols": required_fresh_symbols,

        "rolling_window_seconds": (
            rolling_limits.get(
                "window_seconds"
            )
        ),
        "rolling_trades_used_before": (
            rolling_usage_before.get(
                "trades"
            )
        ),
        "rolling_buys_used_before": (
            rolling_usage_before.get(
                "buys"
            )
        ),
        "rolling_sells_used_before": (
            rolling_usage_before.get(
                "sells"
            )
        ),
        "rolling_buy_notional_used_before": (
            rolling_usage_before.get(
                "buy_notional"
            )
        ),
        "rolling_sell_notional_used_before": (
            rolling_usage_before.get(
                "sell_notional"
            )
        ),
        "rolling_gross_notional_used_before": (
            rolling_usage_before.get(
                "gross_notional"
            )
        ),

        "rolling_trade_limit": (
            rolling_limit_values.get(
                "max_trades"
            )
        ),
        "rolling_buy_limit": (
            rolling_limit_values.get(
                "max_buys"
            )
        ),
        "rolling_sell_limit": (
            rolling_limit_values.get(
                "max_sells"
            )
        ),
        "rolling_buy_notional_limit": (
            rolling_limit_values.get(
                "max_buy_notional"
            )
        ),
        "rolling_sell_notional_limit": (
            rolling_limit_values.get(
                "max_sell_notional"
            )
        ),
        "rolling_gross_notional_limit": (
            rolling_limit_values.get(
                "max_gross_notional"
            )
        ),

        "rolling_trades_authorized": (
            rolling_authorized.get(
                "trades"
            )
        ),
        "rolling_buys_authorized": (
            rolling_authorized.get(
                "buys"
            )
        ),
        "rolling_sells_authorized": (
            rolling_authorized.get(
                "sells"
            )
        ),
        "rolling_buy_notional_authorized": (
            rolling_authorized.get(
                "buy_notional"
            )
        ),
        "rolling_sell_notional_authorized": (
            rolling_authorized.get(
                "sell_notional"
            )
        ),
        "rolling_gross_notional_authorized": (
            rolling_authorized.get(
                "gross_notional"
            )
        ),

        "rolling_limit_adjusted_count": (
            rolling_limits.get(
                "adjusted_count"
            )
        ),
        "rolling_limit_deferred_count": (
            rolling_limits.get(
                "deferred_count"
            )
        ),

        "rest_bar_gate_status": (
            rest_bar_gate.get("status")
        ),
        "rest_bar_candidate_timestamp": (
            rest_bar_gate.get(
                "candidate_bar_timestamp"
            )
        ),
        "rest_bar_wait_attempts": (
            rest_bar_gate.get("attempts")
        ),
        "rest_bar_wait_seconds": (
            rest_bar_gate.get("wait_seconds")
        ),
        "rest_bar_new_symbol_count": (
            rest_bar_gate.get(
                "new_symbol_count"
            )
        ),
        "rest_bar_required_new_symbols": (
            rest_bar_gate.get(
                "required_new_symbols"
            )
        ),
        "rest_bar_new_symbols": (
            rest_bar_gate.get("new_symbols")
        ),
        "rest_bar_duplicate_symbols": (
            rest_bar_gate.get(
                "duplicate_symbols"
            )
        ),
        "rest_bar_lagging_symbols": (
            rest_bar_gate.get(
                "lagging_symbols"
            )
        ),
        "rest_bar_missing_symbols": (
            rest_bar_gate.get(
                "missing_symbols"
            )
        ),
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

        "source_bar_timestamp": (
            layer3_summary.get(
                "source_bar_timestamp"
            )
        ),
        "source_bar_is_new": (
            layer3_summary.get(
                "source_bar_is_new"
            )
        ),
        "target_hysteresis_enabled": (
            target_hysteresis.get(
                "enabled"
            )
        ),
        "target_hysteresis_action_counts": (
            target_hysteresis.get(
                "action_counts"
            )
        ),
        "target_hysteresis_pending_symbols": (
            target_hysteresis.get(
                "pending_symbols"
            )
        ),
        "target_hysteresis_changed_symbols": (
            target_hysteresis.get(
                "changed_symbols"
            )
        ),
        "target_hysteresis_raw_symbol_count": (
            target_hysteresis.get(
                "raw_target_symbol_count"
            )
        ),
        "target_hysteresis_approved_symbol_count": (
            target_hysteresis.get(
                "approved_target_symbol_count"
            )
        ),
        "target_hysteresis_approved_cash_pct": (
            target_hysteresis.get(
                "approved_cash_pct"
            )
        ),

        "open_session_date": layer3_summary.get("open_session_date"),
        "open_session_live_cycle_count": layer3_summary.get("open_session_live_cycle_count"),
        "open_session_reset_seen_symbols": layer3_summary.get("open_session_reset_seen_symbols"),
        "open_session_reset_absent_symbols": layer3_summary.get("open_session_reset_absent_symbols"),

        "opening_transition_active": layer3_summary.get(
            "opening_transition_active"
        ),
        "opening_transition_cycle": layer3_summary.get(
            "opening_transition_cycle"
        ),
        "opening_transition_cycles": layer3_summary.get(
            "opening_transition_cycles"
        ),
        "opening_transition_phase": layer3_summary.get(
            "opening_transition_phase"
        ),
        "opening_transition_source_market_time": layer3_summary.get(
            "opening_transition_source_market_time"
        ),

        "restart_recovery_required": layer3_summary.get(
            "restart_recovery_required"
        ),
        "restart_recovery_active": layer3_summary.get(
            "restart_recovery_active"
        ),
        "restart_recovery_execution_blocked": layer3_summary.get(
            "restart_recovery_execution_blocked"
        ),
        "restart_recovery_reason": layer3_summary.get(
            "restart_recovery_reason"
        ),
        "restart_recovery_warmup_present": layer3_summary.get(
            "restart_recovery_warmup_present"
        ),
        "restart_recovery_warmup_reason": layer3_summary.get(
            "restart_recovery_warmup_reason"
        ),
        "restart_recovery_warmup_trusted": layer3_summary.get(
            "restart_recovery_warmup_trusted"
        ),
        "restart_recovery_warmup_timestamp": layer3_summary.get(
            "restart_recovery_warmup_timestamp"
        ),
        "restart_recovery_warmup_age_minutes": layer3_summary.get(
            "restart_recovery_warmup_age_minutes"
        ),
        "restart_recovery_warmup_snapshot_fallback": layer3_summary.get(
            "restart_recovery_warmup_snapshot_fallback"
        ),
        "restart_recovery_observed_bars": layer3_summary.get(
            "restart_recovery_observed_bars"
        ),
        "restart_recovery_required_bars": layer3_summary.get(
            "restart_recovery_required_bars"
        ),
        "restart_recovery_source_timestamps": layer3_summary.get(
            "restart_recovery_source_timestamps"
        ),
        "restart_recovery_ready_after_source_bar": layer3_summary.get(
            "restart_recovery_ready_after_source_bar"
        ),
        "restart_recovery_completed": layer3_summary.get(
            "restart_recovery_completed"
        ),
        "restart_recovery_evidence_committed": layer3_summary.get(
            "restart_recovery_evidence_committed"
        ),
        "restart_recovery_baseline_seeded": layer3_summary.get(
            "restart_recovery_baseline_seeded"
        ),
        "restart_recovery_baseline_symbols": layer3_summary.get(
            "restart_recovery_baseline_symbols"
        ),
        "restart_recovery_baseline_seeded_this_cycle": layer3_summary.get(
            "restart_recovery_baseline_seeded_this_cycle"
        ),
        "strategy_execution_blocked_reason": layer3_summary.get(
            "strategy_execution_blocked_reason"
        ),

        "bootstrap_confirmation_applied": layer3_summary.get(
            "bootstrap_confirmation_applied"
        ),
        "bootstrap_confirmation_blocked_reason": layer3_summary.get(
            "bootstrap_confirmation_blocked_reason"
        ),
        "bootstrap_confirmation_symbols": layer3_summary.get(
            "bootstrap_confirmation_symbols"
        ),
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

        "equity": equity_value,
        "session_peak_equity": round(peak_equity, 2) if peak_equity > 0 else None,
        "session_peak_timestamp": peak_state.get("timestamp"),
        "equity_giveback_from_peak": round(giveback, 2) if giveback is not None else None,
        "equity_giveback_from_peak_pct": (
            round(giveback / peak_equity, 8)
            if giveback is not None and peak_equity > 0 else None
        ),
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
