# app_state_init.py

import logging
import threading
from collections import deque

from core.state import app_state
from trading.paper_portfolio import PaperPortfolio
from layers.layer2_portfolio import Layer2PortfolioEngine


def ensure_app_state_structure() -> None:
    """Ensure expected nested dicts/containers exist to prevent KeyErrors."""

    main = app_state.setdefault("main", {})
    main.setdefault("symbol", [])
    main.setdefault("async_tasks", set())
    main.setdefault("services", {})
    main.setdefault("starting_equity", None)
    main.setdefault("threads", [])
    main.setdefault("startup_background_task", None)

    app_state.setdefault("paths", {})
    app_state.setdefault("secrets", {})
    app_state.setdefault("log_level", {})

    execution = app_state.setdefault("execution", {})
    execution.setdefault("old_stream_strategy_enabled", False)
    execution.setdefault("layer3_execution_enabled", False)
    execution.setdefault("layer3_market_hours_only", True)
    execution.setdefault("layer3_bootstrap_confirmation_enabled", True)
    execution.setdefault("layer3_bootstrap_min_bar_count", 8)
    execution.setdefault("live_strategy_shadow_rest_bootstrap_enabled", True)

    execution.setdefault("layer_monitor_run_24_7", True)

    execution.setdefault("bar_freshness_market_hours_only", True)
    execution.setdefault("bar_freshness_max_age_minutes", 35.0)
    execution.setdefault("bar_freshness_min_fresh_symbols", 5)
    execution.setdefault("bar_freshness_min_fresh_ratio", 0.70)

    layers = app_state.setdefault("layers", {})
    layers.setdefault("paper_portfolio", None)
    layers.setdefault("engine", None)
    layers.setdefault("latest", {})
    layers.setdefault("rebalance", {})
    layers.setdefault("layer4", {})
    layers.setdefault("layer4_shadow", {})
    layers.setdefault("layer4_execution", {})  # temporary backward-compatible bucket
    layers.setdefault("layer5_execution", {})
    layers.setdefault("active_execution_plan", None)
    layers.setdefault("execution_plan_history", [])

    stream = app_state.setdefault("stream", {})
    stream.setdefault("manager", None)
    stream.setdefault("instance", None)
    stream.setdefault("shutdown_event", threading.Event())
    stream.setdefault("running", False)
    stream.setdefault("stopping", False)
    stream.setdefault("state", "stopped")
    stream.setdefault("loop", None)
    stream.setdefault("thread", None)
    stream.setdefault("lock", threading.Lock())

    debug = stream.setdefault("debug", {})
    debug.setdefault("status", "init")
    debug.setdefault("last_restart", None)
    debug.setdefault("last_trade", None)

    services = app_state.setdefault("services", {})
    services.setdefault("position_tracker", {})
    services.setdefault("balance_tracker", {})
    services.setdefault("order_executor", {})

    fail_safes = app_state.setdefault("fail_safes", {})
    fail_safes.setdefault("state", False)
    fail_safes.setdefault("position_lock", None)
    fail_safes.setdefault("invalid_price_cache", {})
    fail_safes.setdefault("liquidation_in_progress", set())
    fail_safes.setdefault("symbols", set())
    fail_safes.setdefault("pending_liquidation_symbols", [])
    fail_safes.setdefault("liquidate_all", False)
    fail_safes.setdefault("last_trigger_reason", None)
    fail_safes.setdefault("symbol", None)
    fail_safes.setdefault("updated_at", None)

    strategy = app_state.setdefault("strategy", {})
    strategy.setdefault("sells_in_progress", set())
    strategy.setdefault("recent_prices", deque(maxlen=100))
    strategy.setdefault("atr_filter", None)
    strategy.setdefault("volatility_scorer", None)

    app_state.setdefault("open_trades", {})

    portfolio_reconcile = app_state.setdefault("portfolio_reconcile", {})
    portfolio_reconcile.setdefault("running", False)
    portfolio_reconcile.setdefault("broker_snapshot", {})
    portfolio_reconcile.setdefault("last_summary", {})
    portfolio_reconcile.setdefault("last_mismatches", [])
    portfolio_reconcile.setdefault("last_repairs", [])
    portfolio_reconcile.setdefault("last_error", None)

    telegram = app_state.setdefault("telegram", {})
    telegram.setdefault("bot_started", False)
    telegram.setdefault("bot_app", None)
    telegram.setdefault("task", None)
    telegram.setdefault("handle", None)


def initialize_layer_state(top_n: int = 5, force_recreate_engine: bool = False) -> None:
    """
    Initialize long-lived Layer 1/2/3/4 state.

    This should run once during app startup, after app_state["market_data"]["buffer"]
    already exists and before the layer monitor starts.
    """

    market_data_buffer = app_state.get("market_data", {}).get("buffer")

    if market_data_buffer is None:
        raise RuntimeError(
            "Cannot initialize layer state because app_state['market_data']['buffer'] is missing."
        )

    layers = app_state.setdefault("layers", {})

    layers.setdefault("paper_portfolio", PaperPortfolio())

    if force_recreate_engine or layers.get("engine") is None:
        layers["engine"] = Layer2PortfolioEngine(
            market_data_buffer,
            top_n=top_n,
        )

    # Latest Layer 1/2 output.
    layers.setdefault("latest", {})

    # Layer 3 rebalance/planning state.
    rebalance = layers.setdefault("rebalance", {})
    rebalance.setdefault("enabled", True)
    rebalance.setdefault("dry_run", True)
    rebalance.setdefault("last_cycle_id", 0)
    rebalance.setdefault("last_run_at", None)
    rebalance.setdefault("last_plan", [])
    rebalance.setdefault("last_summary", {})
    rebalance.setdefault("target_seen_counts", {})
    rebalance.setdefault("target_absent_counts", {})
    rebalance.setdefault("last_error", None)
    rebalance.setdefault("bootstrap_confirmation_applied", False)
    rebalance.setdefault("bootstrap_confirmation_symbols", [])
    rebalance.setdefault("last_confirmation_update_at", None)
    rebalance.setdefault("confirmation_updates_allowed", None)
    rebalance.setdefault("confirmation_updates_blocked_reason", None)

    # Layer 4 active-plan metadata.
    layer4 = layers.setdefault("layer4", {})
    layer4.setdefault("active_plan_id", None)
    layer4.setdefault("active_plan_expires_at", None)

    # Layer 4 execution status/result tracking.
    layer4_execution = layers.setdefault("layer4_execution", {})
    layer4_execution.setdefault("last_cycle_id", None)
    layer4_execution.setdefault("last_plan_id", None)
    layer4_execution.setdefault("last_attempted_at", None)
    layer4_execution.setdefault("last_result", None)

    layer4_shadow = layers.setdefault("layer4_shadow", {})
    layer4_shadow.setdefault("last_cycle_id", None)
    layer4_shadow.setdefault("last_plan_id", None)
    layer4_shadow.setdefault("last_run_at", None)
    layer4_shadow.setdefault("last_result", None)

    layer5_execution = layers.setdefault("layer5_execution", {})
    layer5_execution.setdefault("last_cycle_id", None)
    layer5_execution.setdefault("last_plan_id", None)
    layer5_execution.setdefault("last_attempted_at", None)
    layer5_execution.setdefault("last_result", None)

    # Current Layer 3 plan that Layer 4 works.
    layers.setdefault("active_execution_plan", None)

    # Small history of replaced/expired execution plans.
    layers.setdefault("execution_plan_history", [])

    layers.setdefault("bar_freshness", {})
    layers.setdefault("last_skipped_evaluation", None)

    logging.info("[Startup] Layer state initialized.")