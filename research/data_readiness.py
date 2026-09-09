"""Explicit fail-closed data readiness report for intraday research."""
from __future__ import annotations


def intraday_data_readiness(*, audit, universe, security_master, market_events,
                            fundamentals, survivorship_safe_declared):
    checks={
        "bars_have_no_duplicates":audit.duplicate_symbol_timestamps==0,
        "bars_have_complete_ohlcv":audit.missing_ohlcv_rows==0,
        "spy_benchmark_present":"SPY" in universe,
        "security_master_complete":bool(security_master.get("complete_observed_coverage")),
        "survivorship_safe_universe":bool(survivorship_safe_declared),
        "halt_luld_history_complete":bool(market_events.get("halt_luld_complete")),
        "corporate_action_history_complete":bool(market_events.get("corporate_actions_complete")),
        "delisting_history_complete":bool(market_events.get("delistings_complete")),
        "point_in_time_market_cap_complete":bool(fundamentals.get("market_cap_complete")),
        "point_in_time_float_complete":bool(fundamentals.get("float_complete")),
    }
    coverage={
        "bar_rows":audit.rows,"symbol_count":audit.symbols,
        "first_timestamp":audit.first_timestamp,"last_timestamp":audit.last_timestamp,
        "security_master_classification_coverage":security_master.get("classification_coverage",0.),
        "fundamental_symbol_session_coverage":fundamentals.get("coverage",0.),
        "market_event_session_count":market_events.get("session_count",0),
        "market_event_missing_coverage_dates":market_events.get("missing_coverage_dates",{}),
    }
    return {"checks":checks,"coverage":coverage,"research_usable":checks["bars_have_no_duplicates"] and checks["bars_have_complete_ohlcv"] and checks["spy_benchmark_present"],
            "promotion_data_ready":all(checks.values()),"missing_requirements":[name for name,value in checks.items() if not value],
            "source_policy":"Only source-provided point-in-time records count as coverage; present-day reconstruction is never treated as historical truth."}
