# state_init.py

import logging

from state import app_state
from paper_portfolio import PaperPortfolio
from layer2_portfolio import Layer2PortfolioEngine


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

    # Current Layer 3 plan that Layer 4 works.
    layers.setdefault("active_execution_plan", None)

    # Small history of replaced/expired execution plans.
    layers.setdefault("execution_plan_history", [])

    logging.info("[Startup] Layer state initialized.")