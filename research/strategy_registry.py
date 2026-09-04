"""Research hypothesis registry independent of live trading configuration.

This module records what is being tested and why. It does not activate a
strategy, place orders, or modify the production strategy roster.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


UTC = timezone.utc
REGISTRY_SCHEMA_VERSION = 1

HYPOTHESES = {
    "TIER1_ETF_TOURNAMENT": {
        "tier": 1, "status": "active_research",
        "mechanism": "compare independent volatility, trend, and sector-rotation hypotheses under identical execution assumptions",
        "data_frequency": "1d", "turnover_expectation": "low_to_medium",
        "point_in_time_equities_required": False,
    },
    "CURRENT_INTRADAY_FAMILY": {
        "tier": "legacy_challenger", "status": "active_challenger",
        "mechanism": "intraday cross-sectional price ranking and target rotation",
        "data_frequency": "5m", "turnover_expectation": "high",
        "point_in_time_equities_required": False,
    },
    "VOL_MANAGED_SPY": {
        "tier": 1, "status": "planned",
        "mechanism": "scale long-only SPY exposure inversely with forecast volatility",
        "data_frequency": "1d", "turnover_expectation": "low",
        "point_in_time_equities_required": False,
    },
    "ETF_DUAL_MOMENTUM": {
        "tier": 1, "status": "planned",
        "mechanism": "combine absolute trend and relative momentum across liquid ETFs",
        "data_frequency": "1d", "turnover_expectation": "low_to_medium",
        "point_in_time_equities_required": False,
    },
    "SECTOR_ETF_ROTATION": {
        "tier": 1, "status": "planned",
        "mechanism": "rotate among sector ETFs subject to an absolute-trend filter",
        "data_frequency": "1d", "turnover_expectation": "medium",
        "point_in_time_equities_required": False,
    },
    "SECTOR_ROTATION_CONCENTRATED": {
        "tier": 2, "status": "active_research",
        "mechanism": "apply the frozen sector momentum signal to one sector to test whether concentration raises return enough to justify higher idiosyncratic risk",
        "data_frequency": "1d", "turnover_expectation": "medium",
        "point_in_time_equities_required": False,
    },
    "SECTOR_ROTATION_INV_VOL": {
        "tier": 2, "status": "active_research",
        "mechanism": "apply the frozen sector momentum selection and inverse-volatility weighting to reduce winner concentration without changing the signal",
        "data_frequency": "1d", "turnover_expectation": "medium",
        "point_in_time_equities_required": False,
    },
    "REGIME_ROUTED_SECTOR": {
        "tier": 2, "status": "active_research",
        "mechanism": "route a fixed sector-momentum sleeve using only lagged SPY trend and realized-volatility states",
        "data_frequency": "1d", "turnover_expectation": "medium",
        "point_in_time_equities_required": False,
    },
    "FACTOR_ETF_MOMENTUM": {
        "tier": 2, "status": "active_research",
        "mechanism": "rotate among liquid long-only factor ETFs using the frozen momentum and absolute-trend signal",
        "data_frequency": "1d", "turnover_expectation": "medium",
        "point_in_time_equities_required": False,
    },
    "INDUSTRY_ETF_MOMENTUM": {
        "tier": 2, "status": "active_research",
        "mechanism": "apply relative and absolute momentum to a fixed liquid industry ETF roster",
        "data_frequency": "1d", "turnover_expectation": "medium",
        "point_in_time_equities_required": False,
    },
    "STATIC_MULTI_SLEEVE": {
        "tier": 2, "status": "active_research",
        "mechanism": "combine sector, factor, and industry momentum sleeves at fixed equal sleeve weights",
        "data_frequency": "1d", "turnover_expectation": "medium",
        "point_in_time_equities_required": False,
    },
    "REGIME_MULTI_SLEEVE": {
        "tier": 2, "status": "active_research",
        "mechanism": "moderately shift a static multi-sleeve portfolio toward defensive assets using lagged trend and volatility",
        "data_frequency": "1d", "turnover_expectation": "medium",
        "point_in_time_equities_required": False,
    },
    "CROSS_ASSET_DUAL_MOMENTUM": {
        "tier": 1, "status": "active_research",
        "mechanism": "combine absolute trend and relative momentum across independently behaving liquid asset classes",
        "data_frequency": "1d", "turnover_expectation": "low_to_medium",
        "point_in_time_equities_required": False,
    },
    "DIVERSIFIED_TREND": {
        "tier": 1, "status": "active_research",
        "mechanism": "equal-weight liquid asset classes with positive long-run trends",
        "data_frequency": "1d", "turnover_expectation": "low",
        "point_in_time_equities_required": False,
    },
    "REGIME_BALANCED": {
        "tier": 1, "status": "active_research",
        "mechanism": "use an equity trend regime to switch between diversified risk and defensive assets",
        "data_frequency": "1d", "turnover_expectation": "low_to_medium",
        "point_in_time_equities_required": False,
    },
    "EQUITY_MOMENTUM_QUALITY": {
        "tier": 2, "status": "blocked_on_data",
        "mechanism": "blend medium-term momentum with point-in-time quality measures",
        "data_frequency": "1d", "turnover_expectation": "medium",
        "point_in_time_equities_required": True,
    },
    "INFORMATION_UNDERREACTION": {
        "tier": 2, "status": "blocked_on_data",
        "mechanism": "trade post-event continuation from timestamped information shocks",
        "data_frequency": "event", "turnover_expectation": "medium",
        "point_in_time_equities_required": True,
    },
    "OVERNIGHT_INTRADAY_SLEEVES": {
        "tier": 2, "status": "diagnostic_first",
        "mechanism": "separate overnight and regular-session return effects",
        "data_frequency": "5m", "turnover_expectation": "medium",
        "point_in_time_equities_required": False,
    },
    "SHORT_TERM_REVERSAL": {
        "tier": 3, "status": "deprioritized",
        "mechanism": "provide liquidity after short-lived price pressure",
        "data_frequency": "intraday", "turnover_expectation": "very_high",
        "point_in_time_equities_required": False,
    },
    "INTRADAY_STRATEGY_ISOLATION": {
        "tier": 2, "status": "active_research",
        "mechanism": "compare opening-range breakout, relative-volume continuation, and VWAP mean reversion independently",
        "data_frequency": "5m", "turnover_expectation": "medium_to_high",
        "point_in_time_equities_required": True,
    },
    "ML_META_ALLOCATOR": {
        "tier": 3, "status": "blocked_on_validated_inputs",
        "mechanism": "combine independently validated strategy sleeves out of sample",
        "data_frequency": "mixed", "turnover_expectation": "model_dependent",
        "point_in_time_equities_required": True,
    },
}


def registry_snapshot() -> dict:
    canonical = json.dumps(HYPOTHESES, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "hypotheses": HYPOTHESES,
    }


def validate_experiment_declaration(experiment: dict | None) -> dict:
    """Validate optional metadata while preserving legacy job compatibility."""
    experiment = dict(experiment or {})
    hypothesis_id = str(experiment.get("hypothesis_id") or "").strip().upper()
    if hypothesis_id and hypothesis_id not in HYPOTHESES:
        raise ValueError(f"unknown hypothesis_id: {hypothesis_id}")
    if hypothesis_id:
        experiment["hypothesis_id"] = hypothesis_id
    trial_id = str(experiment.get("trial_id") or "").strip()
    if trial_id:
        experiment["trial_id"] = trial_id
    return experiment
