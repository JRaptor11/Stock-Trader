# runners/layer_monitor.py

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import time
from datetime import datetime, timezone, timedelta
from statistics import median

from core.market_clock import get_market_is_open
from layers.layer_logging import target_summary_for_log
from layers.layer_schedule import (
    normalize_layer_interval_seconds,
    sleep_until_next_layer_boundary,
)
from layers.layer_bar_gate import (
    accept_distinct_bar_report,
    build_distinct_bar_report,
)
from utils.numeric import safe_float, safe_int

from config import runtime_config as config
from market.bar_data import (
    build_exact_rest_live_bar_comparison,
    fetch_recent_bars_with_min_count,
    filter_fresh_bars,
)

from core.state import app_state
from layers.layer1_ranker import Layer1StockRanker
from layers.layer2_portfolio import Layer2PortfolioBuilder
from layers.layer3_rebalancer import (
    run_layer3_dry_run,
    _get_account_snapshot,
    _get_positions_snapshot,
    build_layer3_shadow_plan,
    source_bar_market_session_info,
)
from layers.layer4_executor import execute_layer4_plan
from layers.layer_csv import (
    append_layer_cycle_row,
    append_layer_live_bar_health_rows,
    append_layer_live_strategy_outcome_rows,
    append_layer_live_strategy_shadow_cycle_row,
    append_layer_live_strategy_shadow_rows,
    append_layer_opening_shadow_cycle_row,
    append_layer_opening_shadow_outcome_rows,
    append_layer_opening_shadow_trade_rows,
    append_layer_strategy_shadow_comparison_row,
    append_layer_strategy_shadow_order_rows,
    append_layer_strategy_shadow_portfolio_rows,
)
from layers.layer_rest_live_attribution import (
    rebuild_rest_live_attribution_csvs,
)
from layers.layer_research_strategy import run_research_strategy_shadow


def _execution_setting(name: str, default):
    raw = app_state.get("execution", {}).get(name, None)

    if raw is not None:
        return raw

    raw = getattr(config, name.upper(), None)

    if raw is not None:
        return raw

    raw = os.getenv(name.upper())

    if raw is not None:
        return raw

    raw = os.getenv(name)

    if raw is not None:
        return raw

    return default


def _execution_bool_setting(name: str, default: bool = False) -> bool:
    """
    Read a boolean setting from:
    1. app_state["execution"][name]
    2. config.NAME
    3. environment variable NAME

    Accepted truthy values:
    true, 1, yes, y, on
    """
    raw = app_state.get("execution", {}).get(name, None)

    if raw is None:
        raw = getattr(config, name.upper(), None)

    if raw is None:
        raw = os.getenv(name.upper())

    if raw is None:
        raw = os.getenv(name)

    if raw is None:
        return bool(default)

    if isinstance(raw, bool):
        return raw

    return str(raw).strip().lower() in {"true", "1", "yes", "y", "on"}


def _expire_active_plan_for_market_close() -> None:
    layers = app_state.setdefault("layers", {})
    active_plan = layers.get("active_execution_plan")

    if isinstance(active_plan, dict) and active_plan.get("status") in {"active", "working"}:
        now = datetime.now(timezone.utc).isoformat()
        active_plan["status"] = "expired_market_closed"
        active_plan["expired_at"] = now
        active_plan["expired_reason"] = "market_closed"

        history = layers.setdefault("execution_plan_history", [])
        history.append(active_plan)
        del history[:-10]

        layers["active_execution_plan"] = None

        layer4 = layers.setdefault("layer4", {})
        layer4["active_plan_id"] = None
        layer4["active_plan_expires_at"] = None

        logging.info(
            "[Layers] Expired active execution plan because market is closed | plan_id=%s",
            active_plan.get("plan_id"),
        )


def _bar_value(bar, key: str, default=None):
    if isinstance(bar, dict):
        return bar.get(key, default)
    return getattr(bar, key, default)


def _latest_rest_bar_timestamp(bar) -> datetime | None:
    raw = _bar_value(bar, "timestamp") or _bar_value(bar, "t")

    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)

    if raw is None:
        return None

    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _latest_close_from_bar(bar) -> float | None:
    value = _bar_value(bar, "close")
    close = safe_float(value, 0.0)
    return close if close > 0 else None


def _build_live_bar_health_rows(
    *,
    symbols: list[str],
    rest_bars_by_symbol: dict,
    market_is_open: bool,
    cycle_id=None,
) -> list[dict]:
    md = app_state.get("market_data", {}).get("buffer")

    if md is None:
        return []

    rows = []
    now = datetime.now(timezone.utc)

    for symbol in symbols:
        try:
            recent_prices = list(md.get_recent_prices(symbol) or [])
            bars_1m = list(md.get_live_bars(symbol, timeframe_seconds=60, limit=10) or []) if hasattr(md, "get_live_bars") else []
            bars_5m = (
                list(
                    md.get_live_bars(
                        symbol,
                        timeframe_seconds=300,
                        limit=12,
                    )
                    or []
                )
                if hasattr(
                    md,
                    "get_live_bars",
                )
                else []
            )
            latest_live_price = safe_float(recent_prices[-1], 0.0) if recent_prices else 0.0
            latest_1m = bars_1m[-1] if bars_1m else None
            latest_5m = bars_5m[-1] if bars_5m else None

            rest_bars = list(rest_bars_by_symbol.get(symbol, []) or [])
            latest_rest = rest_bars[-1] if rest_bars else None
            rest_close = _latest_close_from_bar(latest_rest) if latest_rest is not None else None
            rest_ts = _latest_rest_bar_timestamp(latest_rest) if latest_rest is not None else None

            rest_age_minutes = None
            if rest_ts is not None:
                rest_age_minutes = round(max(0.0, (now - rest_ts).total_seconds() / 60.0), 3)

            live_vs_rest_close_pct = None

            if (
                latest_live_price > 0
                and rest_close
                and rest_close > 0
            ):
                live_vs_rest_close_pct = round(
                    (
                        latest_live_price
                        - rest_close
                    )
                    / rest_close,
                    6,
                )

            exact_bar_comparison = (
                build_exact_rest_live_bar_comparison(
                    rest_bar=latest_rest,
                    live_bars=bars_5m,
                    timeframe_seconds=300,
                )
            )

            rows.append({
                "timestamp": now.isoformat(),
                "cycle_id": cycle_id,
                "symbol": symbol,
                "market_is_open": market_is_open,
                "tick_count": len(recent_prices),
                "live_1m_bar_count": len(bars_1m),
                "live_5m_bar_count": len(bars_5m),
                "latest_live_price": round(latest_live_price, 4) if latest_live_price > 0 else None,
                "latest_1m_close": round(safe_float(_bar_value(latest_1m, "close"), 0.0), 4) if latest_1m else None,
                "latest_5m_close": round(safe_float(_bar_value(latest_5m, "close"), 0.0), 4) if latest_5m else None,
                "latest_1m_volume": round(safe_float(_bar_value(latest_1m, "volume"), 0.0), 2) if latest_1m else None,
                "latest_5m_volume": round(safe_float(_bar_value(latest_5m, "volume"), 0.0), 2) if latest_5m else None,
                "rest_bar_count": len(rest_bars),
                "rest_latest_close": round(rest_close, 4) if rest_close else None,
                "rest_latest_timestamp": rest_ts.isoformat() if rest_ts else None,
                "rest_bar_age_minutes": rest_age_minutes,
                # Legacy diagnostic:
                # newest LIVE tick versus delayed REST close.
                "live_vs_rest_close_pct": (
                    live_vs_rest_close_pct
                ),

                # Exact five-minute bucket comparison.
                **exact_bar_comparison,
            })

        except Exception:
            logging.warning(
                "[Layers] Failed building live-bar health row for %s.",
                symbol,
                exc_info=True,
            )

    return rows


def _live_bar_summary_number(
    value,
) -> float | None:
    if value in (
        None,
        "",
    ):
        return None

    try:
        number = float(
            value
        )
    except Exception:
        return None

    if not math.isfinite(
        number
    ):
        return None

    return number


def _live_bar_summary_values(
    rows: list[dict],
    field: str,
    *,
    absolute: bool = False,
) -> list[float]:
    values = []

    for row in rows or []:
        if not isinstance(
            row,
            dict,
        ):
            continue

        value = (
            _live_bar_summary_number(
                row.get(
                    field
                )
            )
        )

        if value is None:
            continue

        values.append(
            abs(value)
            if absolute
            else value
        )

    return values


def _live_bar_summary_median(
    rows: list[dict],
    field: str,
    *,
    absolute: bool = False,
):
    values = (
        _live_bar_summary_values(
            rows,
            field,
            absolute=absolute,
        )
    )

    if not values:
        return None

    return round(
        median(values),
        8,
    )


def _live_bar_summary_max(
    rows: list[dict],
    field: str,
    *,
    absolute: bool = False,
):
    values = (
        _live_bar_summary_values(
            rows,
            field,
            absolute=absolute,
        )
    )

    if not values:
        return None

    return round(
        max(values),
        8,
    )


def _live_bar_value_counts(
    rows: list[dict],
    field: str,
) -> dict:
    counts = {}

    for row in rows or []:
        if not isinstance(
            row,
            dict,
        ):
            continue

        value = str(
            row.get(field)
            or "UNKNOWN"
        ).strip()

        if not value:
            value = "UNKNOWN"

        counts[value] = (
            counts.get(
                value,
                0,
            )
            + 1
        )

    return dict(
        sorted(
            counts.items()
        )
    )


def _summarize_live_bar_health_rows(
    rows: list[dict],
    *,
    cycle_id=None,
) -> dict:
    """
    Build one cycle-level quality summary from the exact
    timestamp-matched REST/LIVE health rows.
    """
    valid_rows = [
        row
        for row in (
            rows or []
        )
        if isinstance(
            row,
            dict,
        )
    ]

    symbol_count = len(
        valid_rows
    )

    exact_rows = [
        row
        for row in valid_rows
        if bool(
            row.get(
                "bar_match_exact"
            )
        )
    ]

    eligible_rows = [
        row
        for row in valid_rows
        if bool(
            row.get(
                "bar_match_comparison_eligible"
            )
        )
    ]

    full_capture_rows = [
        row
        for row in eligible_rows
        if bool(
            row.get(
                "bar_match_live_full_capture_candidate"
            )
        )
    ]

    unmatched_symbols = sorted({
        str(
            row.get("symbol")
            or ""
        ).upper().strip()
        for row in valid_rows
        if not bool(
            row.get(
                "bar_match_exact"
            )
        )
        and str(
            row.get("symbol")
            or ""
        ).strip()
    })

    non_full_capture_symbols = sorted({
        str(
            row.get("symbol")
            or ""
        ).upper().strip()
        for row in eligible_rows
        if not bool(
            row.get(
                "bar_match_live_full_capture_candidate"
            )
        )
        and str(
            row.get("symbol")
            or ""
        ).strip()
    })

    exact_match_count = len(
        exact_rows
    )

    comparison_eligible_count = len(
        eligible_rows
    )

    full_capture_count = len(
        full_capture_rows
    )

    summary = {
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "cycle_id": cycle_id,

        "live_bar_health_symbol_count": (
            symbol_count
        ),
        "live_bar_exact_match_count": (
            exact_match_count
        ),
        "live_bar_comparison_eligible_count": (
            comparison_eligible_count
        ),
        "live_bar_full_capture_candidate_count": (
            full_capture_count
        ),

        "live_bar_exact_match_rate": (
            round(
                exact_match_count
                / symbol_count,
                6,
            )
            if symbol_count
            else None
        ),

        "live_bar_comparison_eligible_rate": (
            round(
                comparison_eligible_count
                / symbol_count,
                6,
            )
            if symbol_count
            else None
        ),

        "live_bar_full_capture_rate": (
            round(
                full_capture_count
                / comparison_eligible_count,
                6,
            )
            if comparison_eligible_count
            else None
        ),

        "live_bar_match_status_counts": (
            _live_bar_value_counts(
                valid_rows,
                "bar_match_status",
            )
        ),

        "live_bar_capture_quality_counts": (
            _live_bar_value_counts(
                exact_rows,
                "bar_match_live_capture_quality",
            )
        ),

        "live_bar_unmatched_symbols": (
            unmatched_symbols
        ),

        "live_bar_non_full_capture_symbols": (
            non_full_capture_symbols
        ),

        "live_bar_median_abs_open_pct_delta": (
            _live_bar_summary_median(
                eligible_rows,
                "open_pct_delta_live_minus_rest",
                absolute=True,
            )
        ),

        "live_bar_median_abs_high_pct_delta": (
            _live_bar_summary_median(
                eligible_rows,
                "high_pct_delta_live_minus_rest",
                absolute=True,
            )
        ),

        "live_bar_median_abs_low_pct_delta": (
            _live_bar_summary_median(
                eligible_rows,
                "low_pct_delta_live_minus_rest",
                absolute=True,
            )
        ),

        "live_bar_median_abs_close_pct_delta": (
            _live_bar_summary_median(
                eligible_rows,
                "close_pct_delta_live_minus_rest",
                absolute=True,
            )
        ),

        "live_bar_max_abs_close_pct_delta": (
            _live_bar_summary_max(
                eligible_rows,
                "close_pct_delta_live_minus_rest",
                absolute=True,
            )
        ),

        "live_bar_median_volume_capture_ratio": (
            _live_bar_summary_median(
                eligible_rows,
                "volume_capture_ratio_live_to_rest",
            )
        ),

        "live_bar_median_trade_count_capture_ratio": (
            _live_bar_summary_median(
                eligible_rows,
                "trade_count_capture_ratio_live_to_rest",
            )
        ),
    }

    return summary


def _append_live_bar_health_snapshot(
    *,
    symbols: list[str],
    rest_bars_by_symbol: dict,
    market_is_open: bool,
    cycle_id=None,
) -> dict:
    rows = _build_live_bar_health_rows(
        symbols=symbols,
        rest_bars_by_symbol=(
            rest_bars_by_symbol
        ),
        market_is_open=market_is_open,
        cycle_id=cycle_id,
    )

    summary = (
        _summarize_live_bar_health_rows(
            rows,
            cycle_id=cycle_id,
        )
    )

    app_state.setdefault(
        "layers",
        {},
    )[
        "latest_live_bar_health_summary"
    ] = dict(summary)

    if not rows:
        logging.info(
            "[LiveBarMatch] No health rows | "
            "cycle_id=%s",
            cycle_id,
        )

        return summary

    append_layer_live_bar_health_rows(
        rows
    )

    logging.info(
        "[LiveBarMatch] Cycle summary | "
        "cycle_id=%s exact=%s/%s "
        "eligible=%s/%s full_capture=%s/%s "
        "median_abs_close_pct=%s "
        "median_volume_capture=%s "
        "statuses=%s",
        cycle_id,
        summary.get(
            "live_bar_exact_match_count"
        ),
        summary.get(
            "live_bar_health_symbol_count"
        ),
        summary.get(
            "live_bar_comparison_eligible_count"
        ),
        summary.get(
            "live_bar_health_symbol_count"
        ),
        summary.get(
            "live_bar_full_capture_candidate_count"
        ),
        summary.get(
            "live_bar_comparison_eligible_count"
        ),
        summary.get(
            "live_bar_median_abs_close_pct_delta"
        ),
        summary.get(
            "live_bar_median_volume_capture_ratio"
        ),
        summary.get(
            "live_bar_match_status_counts"
        ),
    )

    compact = {
        row["symbol"]: {
            "match": row.get(
                "bar_match_status"
            ),
            "quality": row.get(
                "bar_match_live_capture_quality"
            ),
            "close_pct": row.get(
                "close_pct_delta_live_minus_rest"
            ),
            "volume_ratio": row.get(
                "volume_capture_ratio_live_to_rest"
            ),
        }
        for row in rows
    }

    logging.debug(
        "[LiveBarMatch] Symbol details: %s",
        compact,
    )

    return summary


def _fresh_symbol_requirement(symbol_count: int) -> int:
    min_symbols = safe_int(
        _execution_setting("bar_freshness_min_fresh_symbols", 5),
        5,
    )

    min_ratio = safe_float(
        _execution_setting("bar_freshness_min_fresh_ratio", 0.70),
        0.70,
    )

    ratio_required = math.ceil(symbol_count * min_ratio)

    return max(1, min(symbol_count, max(min_symbols, ratio_required)))


def _layer2_evaluation_context(
    market_is_open: bool,
    *,
    count_live_cycle: bool,
    source_bar_timestamp=None,
) -> dict:
    """
    Build Layer 2 smoothing context from the accepted source bar.

    Opening transition is a market-data phase, not a process-lifetime counter.
    A mid-session restart therefore cannot restart opening smoothing at cycle 1.
    """
    layers = app_state.setdefault(
        "layers",
        {},
    )

    state = layers.setdefault(
        "opening_transition",
        {},
    )

    transition_cycles = safe_int(
        _execution_setting(
            "layer2_opening_transition_smoothing_cycles",
            6,
        ),
        6,
    )

    session_info = source_bar_market_session_info(
        source_bar_timestamp,
        transition_cycles=transition_cycles,
    )

    active = bool(
        market_is_open
        and session_info.get(
            "opening_transition_active"
        )
    )

    state.update({
        "date": session_info.get(
            "session_date"
        ),
        "source_bar_timestamp": session_info.get(
            "source_bar_timestamp"
        ),
        "source_bar_market_time": session_info.get(
            "source_bar_market_time"
        ),
        "source_phase": session_info.get(
            "phase"
        ),
        "source_opening_cycle": session_info.get(
            "opening_transition_cycle"
        ),
        "transition_cycles": transition_cycles,
        "active": active,
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(),
    })

    return {
        "market_is_open": bool(
            market_is_open
        ),
        "opening_transition_active": active,
        "opening_transition_cycle": session_info.get(
            "opening_transition_cycle"
        ),
        "opening_transition_cycles": transition_cycles,
        "opening_transition_phase": session_info.get(
            "phase"
        ),
        "opening_transition_source_market_time": (
            session_info.get(
                "source_bar_market_time"
            )
        ),
    }


def _off_hours_warmup_diagnostics() -> dict:
    warmup = app_state.get(
        "layers",
        {},
    ).get(
        "last_off_hours_warmup"
    )

    if not isinstance(
        warmup,
        dict,
    ):
        warmup = {}

    target = warmup.get(
        "target_portfolio"
    )

    reason = str(
        warmup.get("reason")
        or ""
    ).strip()

    timestamp = warmup.get(
        "timestamp"
    )

    age_minutes = None

    if timestamp:
        try:
            parsed = datetime.fromisoformat(
                str(timestamp).replace(
                    "Z",
                    "+00:00",
                )
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(
                    tzinfo=timezone.utc
                )

            age_minutes = round(
                max(
                    0.0,
                    (
                        datetime.now(
                            timezone.utc
                        )
                        - parsed
                    ).total_seconds()
                    / 60.0,
                ),
                3,
            )

        except Exception:
            age_minutes = None

    freshness_report = warmup.get(
        "freshness_report"
    )

    if not isinstance(
        freshness_report,
        dict,
    ):
        freshness_report = {}

    present = bool(warmup)

    available = bool(
        isinstance(
            target,
            dict,
        )
        and bool(target)
    )

    trusted = bool(
        reason
        == "market_closed_target_warmup"
        and available
    )

    snapshot_fallback = bool(
        freshness_report.get(
            "warmup_snapshot_fallback"
        )
        or reason in {
            "market_open_rest_snapshot_fallback",
            "market_closed_rest_snapshot_fallback",
        }
    )

    return {
        "present": present,
        "available": available,
        "reason": reason or None,
        "trusted_for_restart_recovery": (
            trusted
        ),
        "timestamp": timestamp,
        "age_minutes": age_minutes,
        "snapshot_fallback": (
            snapshot_fallback
        ),
    }


def _usable_off_hours_warmup_available() -> bool:
    return bool(
        _off_hours_warmup_diagnostics().get(
            "trusted_for_restart_recovery"
        )
    )


def _restart_recovery_context(
    *,
    market_is_open: bool,
    source_bar_timestamp=None,
    advance: bool = False,
) -> dict:
    """
    Require two successfully planned distinct cohorts after a cold restart.

    The preview call (advance=False) determines whether the current cohort must
    be blocked without consuming recovery evidence. After Layer 3 completes
    successfully, advance=True commits that source cohort. Failed evaluations
    therefore cannot accidentally satisfy the recovery window.
    """
    enabled = _execution_bool_setting(
        "layer3_restart_recovery_enabled",
        True,
    )

    required_bars = max(
        1,
        safe_int(
            _execution_setting(
                "layer3_restart_recovery_bars",
                2,
            ),
            2,
        ),
    )

    transition_cycles = safe_int(
        _execution_setting(
            "layer3_opening_transition_cycles",
            6,
        ),
        6,
    )

    session_info = source_bar_market_session_info(
        source_bar_timestamp,
        transition_cycles=transition_cycles,
    )

    layers = app_state.setdefault(
        "layers",
        {},
    )

    state = layers.setdefault(
        "restart_recovery",
        {},
    )

    warmup_diagnostics = (
        _off_hours_warmup_diagnostics()
    )

    if not market_is_open:
        return {
            "enabled": enabled,
            "required": False,
            "active": False,
            "execution_blocked": False,
            "reason": "market_closed",
            "warmup_present": (
                warmup_diagnostics.get(
                    "present"
                )
            ),
            "warmup_reason": (
                warmup_diagnostics.get(
                    "reason"
                )
            ),
            "warmup_trusted_for_restart_recovery": (
                warmup_diagnostics.get(
                    "trusted_for_restart_recovery"
                )
            ),
            "warmup_timestamp": (
                warmup_diagnostics.get(
                    "timestamp"
                )
            ),
            "warmup_age_minutes": (
                warmup_diagnostics.get(
                    "age_minutes"
                )
            ),
            "warmup_snapshot_fallback": (
                warmup_diagnostics.get(
                    "snapshot_fallback"
                )
            ),
            "required_bars": required_bars,
            "observed_bars": safe_int(
                state.get(
                    "observed_bars",
                    0,
                ),
                0,
            ),
            "source_timestamps": list(
                state.get(
                    "source_timestamps",
                    [],
                )
                or []
            ),
            "source_session": session_info,
        }

    session_date = (
        session_info.get(
            "session_date"
        )
        or datetime.now(
            timezone.utc
        ).date().isoformat()
    )

    if state.get(
        "session_date"
    ) != session_date:
        warmup_available = (
            _usable_off_hours_warmup_available()
        )

        required = bool(
            enabled
            and not warmup_available
        )

        state.clear()
        state.update({
            "session_date": session_date,
            "enabled": enabled,
            "required": required,
            "reason": (
                "missing_off_hours_warmup"
                if required
                else (
                    "warmup_available"
                    if warmup_available
                    else "restart_recovery_disabled"
                )
            ),
            "warmup_available": (
                warmup_available
            ),
            "required_bars": required_bars,
            "observed_bars": 0,
            "source_timestamps": [],
            "completed": not required,
            "started_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "completed_at": None,
            "ready_after_source_bar": None,
        })

        logging.warning(
            "[Layer3Recovery] Session recovery initialized | "
            "session_date=%s required=%s reason=%s required_bars=%s "
            "source_phase=%s source_opening_cycle=%s",
            session_date,
            required,
            state.get("reason"),
            required_bars,
            session_info.get("phase"),
            session_info.get(
                "opening_transition_cycle"
            ),
        )

    required = bool(
        state.get("required")
    )

    completed_before_cycle = bool(
        state.get("completed")
    )

    source_timestamp = session_info.get(
        "source_bar_timestamp"
    )

    committed_timestamps = list(
        state.get(
            "source_timestamps",
            [],
        )
        or []
    )

    source_is_new = bool(
        source_timestamp
        and source_timestamp
        not in committed_timestamps
    )

    blocked_this_cycle = bool(
        required
        and not completed_before_cycle
    )

    prospective_timestamps = list(
        committed_timestamps
    )

    if (
        blocked_this_cycle
        and source_is_new
    ):
        prospective_timestamps.append(
            source_timestamp
        )

    prospective_timestamps = (
        prospective_timestamps[
            -required_bars:
        ]
    )

    prospective_observed = len(
        prospective_timestamps
    )

    if (
        advance
        and blocked_this_cycle
        and source_is_new
    ):
        state["source_timestamps"] = (
            prospective_timestamps
        )

        state["observed_bars"] = (
            prospective_observed
        )

        state[
            "last_committed_source_bar_timestamp"
        ] = source_timestamp

        state["last_committed_at"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        if (
            prospective_observed
            >= required_bars
        ):
            state["completed"] = True
            state["completed_at"] = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )
            state[
                "ready_after_source_bar"
            ] = source_timestamp

            logging.warning(
                "[Layer3Recovery] Recovery evidence complete; "
                "ordinary execution remains blocked for this cohort and "
                "resumes on the next distinct source bar | "
                "observed=%s/%s ready_after=%s",
                prospective_observed,
                required_bars,
                source_timestamp,
            )

    state[
        "last_source_bar_timestamp"
    ] = source_timestamp

    state["last_source_phase"] = (
        session_info.get("phase")
    )

    state["updated_at"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    observed_for_context = (
        prospective_observed
        if blocked_this_cycle
        else safe_int(
            state.get(
                "observed_bars",
                0,
            ),
            0,
        )
    )

    timestamps_for_context = (
        prospective_timestamps
        if blocked_this_cycle
        else list(
            state.get(
                "source_timestamps",
                [],
            )
            or []
        )
    )

    context = {
        "enabled": bool(
            state.get("enabled")
        ),
        "required": required,
        "active": blocked_this_cycle,
        "execution_blocked": (
            blocked_this_cycle
        ),
        "reason": state.get("reason"),
        "warmup_available": bool(
            state.get(
                "warmup_available"
            )
        ),
        "warmup_present": bool(
            state.get(
                "warmup_present"
            )
        ),
        "warmup_reason": state.get(
            "warmup_reason"
        ),
        "warmup_trusted_for_restart_recovery": bool(
            state.get(
                "warmup_trusted_for_restart_recovery"
            )
        ),
        "warmup_timestamp": state.get(
            "warmup_timestamp"
        ),
        "warmup_age_minutes": state.get(
            "warmup_age_minutes"
        ),
        "warmup_snapshot_fallback": bool(
            state.get(
                "warmup_snapshot_fallback"
            )
        ),
        "required_bars": safe_int(
            state.get(
                "required_bars",
                required_bars,
            ),
            required_bars,
        ),
        "observed_bars": (
            observed_for_context
        ),
        "source_timestamps": (
            timestamps_for_context
        ),
        "source_bar_is_new": (
            source_is_new
        ),
        "evidence_committed": bool(
            advance
            and source_is_new
        ),
        "completed": bool(
            state.get("completed")
        ),
        "ready_after_source_bar": state.get(
            "ready_after_source_bar"
        ),
        "source_session": session_info,
    }

    if (
        blocked_this_cycle
        and not advance
    ):
        logging.warning(
            "[Layer3Recovery] Ordinary strategy execution blocked | "
            "prospective_observed=%s/%s source=%s phase=%s",
            observed_for_context,
            required_bars,
            source_timestamp,
            session_info.get("phase"),
        )

    return context


def store_latest_layer_result(symbols, bar_counts, ranked, target):
    """
    Store the latest Layer 1/2 result in app_state so Layer 3 can read it.

    This function does not place trades.
    It only creates the handoff from Layer 1/2 target generation
    to Layer 3 planning/execution.
    """
    layers = app_state.setdefault("layers", {})

    ranked_snapshot = []
    for r in ranked or []:
        ranked_snapshot.append({
            "symbol": getattr(r, "symbol", None),
            "score": float(getattr(r, "score", 0.0) or 0.0),
            "last_price": float(getattr(r, "last_price", 0.0) or 0.0),
            "reason": getattr(r, "reason", ""),
        })

    target = target or {}
    target_meta = target.get("_meta", {}) if isinstance(target, dict) else {}

    latest = layers.setdefault("latest", {})
    latest["timestamp"] = datetime.now(timezone.utc).isoformat()
    latest["symbols_evaluated"] = list(symbols or [])
    latest["bar_counts"] = dict(bar_counts or {})
    latest["ranked"] = ranked_snapshot
    latest["target_portfolio"] = dict(target)
    latest["target_meta"] = dict(target_meta)

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

    logging.info(
        "[Layers] Stored latest Layer 1/2 result for Layer 3 | ranked_count=%s target_summary=%s",
        len(ranked_snapshot),
        target_summary_for_log(target),
    )


def store_off_hours_layer_warmup_result(
    symbols,
    bar_counts,
    ranked,
    target,
    freshness_report,
    *,
    reason: str = "market_closed_target_warmup",
    rest_bar_gate: dict | None = None,
):
    """
    Store a non-executable Layer 1/2 warmup target.

    This intentionally does not update app_state["layers"]["latest"], because
    that object is the Layer 3 handoff used for executable planning.

    The warmup still matters because layer_engine.evaluate() updates Layer 2's
    internal previous_target_portfolio, allowing the first executable target
    to be smoothed against an existing strategic baseline.
    """
    layers = app_state.setdefault(
        "layers",
        {},
    )

    ranked_snapshot = []

    for row in ranked or []:
        ranked_snapshot.append({
            "symbol": getattr(
                row,
                "symbol",
                None,
            ),
            "score": float(
                getattr(
                    row,
                    "score",
                    0.0,
                )
                or 0.0
            ),
            "last_price": float(
                getattr(
                    row,
                    "last_price",
                    0.0,
                )
                or 0.0
            ),
            "reason": getattr(
                row,
                "reason",
                "",
            ),
        })

    target = target or {}

    target_meta = (
        target.get(
            "_meta",
            {},
        )
        if isinstance(
            target,
            dict,
        )
        else {}
    )

    layers["last_off_hours_warmup"] = {
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "reason": reason,
        "executable": False,
        "symbols_evaluated": list(
            symbols or []
        ),
        "bar_counts": dict(
            bar_counts or {}
        ),
        "ranked": ranked_snapshot,
        "target_portfolio": dict(
            target
        ),
        "target_meta": dict(
            target_meta
        ),
        "freshness_report": dict(
            freshness_report or {}
        ),
        "rest_bar_gate": dict(
            rest_bar_gate or {}
        ),
    }

    logging.info(
        "[Layers] Stored non-executable Layer 1/2 warmup target | "
        "reason=%s ranked_count=%s target_summary=%s",
        reason,
        len(ranked_snapshot),
        target_summary_for_log(
            target
        ),
    )


def _warmup_target_available() -> bool:
    warmup = (
        app_state.get(
            "layers",
            {},
        )
        .get(
            "last_off_hours_warmup"
        )
    )

    if not isinstance(
        warmup,
        dict,
    ):
        return False

    target = warmup.get(
        "target_portfolio"
    )

    return bool(
        isinstance(
            target,
            dict,
        )
        and target
    )


def _try_store_rest_snapshot_warmup(
    *,
    symbols: list[str],
    bars_by_symbol: dict,
    freshness_report: dict | None,
    required_symbols: int,
    reason: str,
    rest_bar_gate: dict | None = None,
) -> dict:
    """
    Build one non-executable Layer 1/2 baseline when no warmup exists.

    This fallback intentionally does not:

    - update the executable Layer 1/2 handoff
    - call Layer 3
    - call Layer 4
    - accept the REST bar gate
    - increment the opening live-cycle count

    It only seeds Layer 2 smoothing and stores a warmup snapshot.
    """
    if _warmup_target_available():
        return {
            "status": (
                "existing_warmup_kept"
            ),
            "stored": False,
        }

    layer_engine = (
        app_state.get(
            "layers",
            {},
        )
        .get(
            "engine"
        )
    )

    if layer_engine is None:
        return {
            "status": (
                "missing_layer_engine"
            ),
            "stored": False,
        }

    minimum_bars = max(
        1,
        safe_int(
            _execution_setting(
                "layer_warmup_snapshot_min_bars",
                180,
            ),
            180,
        ),
    )

    eligible_symbols = [
        str(
            symbol or ""
        ).upper().strip()
        for symbol in symbols or []
        if (
            str(
                symbol or ""
            ).upper().strip()
            and len(
                list(
                    (
                        bars_by_symbol
                        or {}
                    ).get(
                        str(
                            symbol or ""
                        ).upper().strip(),
                        [],
                    )
                    or []
                )
            )
            >= minimum_bars
        )
    ]

    required_symbols = max(
        1,
        min(
            len(
                symbols or []
            ),
            safe_int(
                required_symbols,
                1,
            ),
        ),
    )

    if (
        len(
            eligible_symbols
        )
        < required_symbols
    ):
        return {
            "status": (
                "insufficient_snapshot_bars"
            ),
            "stored": False,
            "eligible_symbol_count": len(
                eligible_symbols
            ),
            "required_symbol_count": (
                required_symbols
            ),
            "minimum_bars": (
                minimum_bars
            ),
        }

    evaluation_bars = {
        symbol: list(
            (
                bars_by_symbol
                or {}
            ).get(
                symbol,
                [],
            )
            or []
        )
        for symbol in eligible_symbols
    }

    result = layer_engine.evaluate(
        eligible_symbols,
        bars_by_symbol=(
            evaluation_bars
        ),
        context=(
            _layer2_evaluation_context(
                market_is_open=False,
                count_live_cycle=False,
            )
        ),
    )

    ranked = result.get(
        "ranked",
        [],
    )

    target = result.get(
        "target_portfolio",
        {},
    )

    if (
        not isinstance(
            target,
            dict,
        )
        or not target
    ):
        return {
            "status": (
                "snapshot_evaluation_no_target"
            ),
            "stored": False,
            "eligible_symbol_count": len(
                eligible_symbols
            ),
        }

    stored_freshness_report = dict(
        freshness_report or {}
    )

    stored_freshness_report.update({
        "warmup_snapshot_fallback": True,
        "warmup_snapshot_reason": (
            reason
        ),
        "warmup_snapshot_min_bars": (
            minimum_bars
        ),
        "warmup_snapshot_gate_status": (
            (
                rest_bar_gate
                or {}
            ).get(
                "status"
            )
        ),
    })

    bar_counts = {
        symbol: len(
            evaluation_bars.get(
                symbol,
                [],
            )
        )
        for symbol in eligible_symbols
    }

    store_off_hours_layer_warmup_result(
        symbols=eligible_symbols,
        bar_counts=bar_counts,
        ranked=ranked,
        target=target,
        freshness_report=(
            stored_freshness_report
        ),
        reason=reason,
        rest_bar_gate=(
            rest_bar_gate
        ),
    )

    app_state.setdefault(
        "layers",
        {},
    )[
        "last_rest_snapshot_warmup_fallback"
    ] = {
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "reason": reason,
        "eligible_symbols": list(
            eligible_symbols
        ),
        "minimum_bars": (
            minimum_bars
        ),
        "gate_status": (
            (
                rest_bar_gate
                or {}
            ).get(
                "status"
            )
        ),
    }

    logging.info(
        "[Layers] REST snapshot warmup fallback stored | "
        "reason=%s eligible=%s/%s min_bars=%s",
        reason,
        len(
            eligible_symbols
        ),
        len(
            symbols or []
        ),
        minimum_bars,
    )

    return {
        "status": "stored",
        "stored": True,
        "reason": reason,
        "eligible_symbol_count": len(
            eligible_symbols
        ),
        "required_symbol_count": (
            required_symbols
        ),
        "minimum_bars": (
            minimum_bars
        ),
    }


# ================================================================
# LIVE-BAR LAYER 1/2 SHADOW COMPARISON
# ---------------------------------------------------------------
# This does NOT trade.
# It exists only to compare:
#   current REST/delayed-bar Layer 1/2 target vs live-bar Layer 1/2 target
# ================================================================


def _target_meta(target: dict | None) -> dict:
    if not isinstance(target, dict):
        return {}
    meta = target.get("_meta", {})
    return meta if isinstance(meta, dict) else {}


def _target_weight(target: dict | None, symbol: str) -> float:
    if not isinstance(target, dict):
        return 0.0
    return safe_float(target.get(symbol), 0.0)


def _target_symbols(target: dict | None) -> set[str]:
    if not isinstance(target, dict):
        return set()

    out = set()
    for raw_symbol, raw_weight in target.items():
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol or symbol in {"CASH", "USD", "_META"} or symbol.startswith("_"):
            continue
        if safe_float(raw_weight, 0.0) > 0:
            out.add(symbol)
    return out


def _rank_snapshot(ranked: list | None) -> tuple[dict, list[str]]:
    rank_map = {}
    top_symbols = []

    for index, row in enumerate(ranked or [], start=1):
        symbol = str(getattr(row, "symbol", "") or "").upper().strip()
        if not symbol:
            continue

        if len(top_symbols) < 5:
            top_symbols.append(symbol)

        rank_map[symbol] = {
            "rank": index,
            "score": safe_float(getattr(row, "score", 0.0), 0.0),
            "last_price": safe_float(getattr(row, "last_price", 0.0), 0.0),
            "reason": getattr(row, "reason", ""),
        }

    return rank_map, top_symbols


def _latest_live_price_for_symbol(md, symbol: str, live_bars: list[dict] | None = None) -> float:
    try:
        recent_prices = list(md.get_recent_prices(symbol, limit=1) or []) if md is not None else []
        if recent_prices:
            price = safe_float(recent_prices[-1], 0.0)
            if price > 0:
                return price
    except Exception:
        pass

    try:
        if live_bars:
            price = safe_float(live_bars[-1].get("close"), 0.0)
            if price > 0:
                return price
    except Exception:
        pass

    return 0.0


def _live_bar_is_complete(
    bar: dict,
    timeframe_seconds: int,
    now_epoch: float,
) -> bool:
    """
    Return True only for strategy-eligible local bars.

    Local bars must:
    - have passed the lateness grace period
    - be sealed
    - have continuous full-interval stream coverage
    - contain no receipt-time timestamp fallbacks
    """
    try:
        bucket_start = safe_float(
            _bar_value(
                bar,
                "bucket_start",
            ),
            0.0,
        )

        if bucket_start > 0:
            strategy_eligible_at = (
                safe_float(
                    _bar_value(
                        bar,
                        "strategy_eligible_at_epoch",
                    ),
                    (
                        bucket_start
                        + timeframe_seconds
                    ),
                )
            )

            if now_epoch < strategy_eligible_at:
                return False

            if not bool(
                _bar_value(
                    bar,
                    "sealed",
                )
            ):
                return False

            capture_quality = str(
                _bar_value(
                    bar,
                    "capture_quality",
                )
                or ""
            ).upper().strip()

            if (
                capture_quality
                != "FULL_CAPTURE_CANDIDATE"
            ):
                return False

            if safe_int(
                _bar_value(
                    bar,
                    "event_timestamp_fallback_count",
                ),
                0,
            ) > 0:
                return False

            return True

    except Exception:
        pass

    # REST bootstrap bars do not contain local construction metadata.
    try:
        ts = _latest_rest_bar_timestamp(
            bar
        )

        if ts is not None:
            return (
                ts.timestamp()
                + timeframe_seconds
            ) <= now_epoch

    except Exception:
        pass

    return False


def _bar_epoch_seconds(bar: dict) -> float | None:
    """
    Return a comparable epoch timestamp for REST bars or locally built live bars.
    """
    try:
        bucket_start = safe_float(_bar_value(bar, "bucket_start"), 0.0)
        if bucket_start > 0:
            return float(bucket_start)
    except Exception:
        pass

    try:
        ts = _latest_rest_bar_timestamp(bar)
        if ts is not None:
            return float(ts.timestamp())
    except Exception:
        pass

    return None


def _latest_source_bar_timestamp(
    bars_by_symbol: dict,
    symbols: list[str] | None = None,
) -> str | None:
    """
    Return the newest completed input-bar timestamp used by one source.
    """
    symbol_list = (
        symbols
        or list(
            (bars_by_symbol or {}).keys()
        )
    )

    latest_epoch = None

    for symbol in symbol_list:
        bars = list(
            (bars_by_symbol or {}).get(
                symbol,
                [],
            )
            or []
        )

        if not bars:
            continue

        epoch = _bar_epoch_seconds(
            bars[-1]
        )

        if epoch is None:
            continue

        latest_epoch = (
            epoch
            if latest_epoch is None
            else max(
                latest_epoch,
                epoch,
            )
        )

    if latest_epoch is None:
        return None

    return datetime.fromtimestamp(
        latest_epoch,
        tz=timezone.utc,
    ).isoformat()


def _build_live_bars_by_symbol(
    symbols: list[str],
    *,
    timeframe_seconds: int,
    limit: int,
    rest_bars_by_symbol: dict | None = None,
    rest_bootstrap_enabled: bool = False,
) -> tuple[dict, dict, dict]:
    md = app_state.get("market_data", {}).get("buffer")
    if md is None or not hasattr(md, "get_live_bars"):
        return {}, {}, {}

    rest_bars_by_symbol = rest_bars_by_symbol or {}

    bars_by_symbol = {}
    bar_counts = {}
    live_prices = {}
    now_epoch = time.time()

    for symbol in symbols or []:
        symbol = str(symbol or "").upper().strip()
        if not symbol:
            continue

        try:
            raw_live_bars = list(
                md.get_live_bars(
                    symbol,
                    timeframe_seconds=timeframe_seconds,
                    limit=limit,
                ) or []
            )

            completed_live_bars = [
                bar
                for bar in raw_live_bars
                if _live_bar_is_complete(bar, timeframe_seconds, now_epoch)
            ]

            if rest_bootstrap_enabled:
                bootstrap_bars = []

                for bar in list(rest_bars_by_symbol.get(symbol, []) or []):
                    bar_epoch = _bar_epoch_seconds(bar)
                    if bar_epoch is None:
                        continue

                    # REST bars should already be completed, but enforce the
                    # same completed-bar rule for a cleaner comparison.
                    if (bar_epoch + timeframe_seconds) <= now_epoch:
                        bootstrap_bars.append(bar)

                if bootstrap_bars:
                    latest_bootstrap_epoch = max(
                        (_bar_epoch_seconds(bar) or 0.0)
                        for bar in bootstrap_bars
                    )

                    live_after_bootstrap = [
                        bar
                        for bar in completed_live_bars
                        if (_bar_epoch_seconds(bar) or 0.0) > latest_bootstrap_epoch
                    ]

                    bars = bootstrap_bars + live_after_bootstrap
                else:
                    bars = completed_live_bars
            else:
                bars = completed_live_bars

            if limit and limit > 0:
                bars = bars[-limit:]

        except Exception:
            logging.warning(
                "[LiveStrategyShadow] Failed reading live bars for %s.",
                symbol,
                exc_info=True,
            )
            bars = []

        bars_by_symbol[symbol] = bars
        bar_counts[symbol] = len(bars)

        # Use the close of the latest completed strategy input bar.
        # With bootstrap ON, this may initially be a REST close until newer
        # local live bars exist. With bootstrap OFF, this is pure local-live.
        if bars:
            live_prices[symbol] = safe_float(_bar_value(bars[-1], "close"), 0.0)
        else:
            live_prices[symbol] = 0.0

    return bars_by_symbol, bar_counts, live_prices


def _latest_completed_live_cohort(
    bars_by_symbol: dict,
    *,
    timeframe_seconds: int,
    required_symbols: int,
) -> dict:
    """
    Find the newest completed local-live bar bucket shared by enough symbols.

    Only bars containing bucket_start count as internally constructed LIVE
    bars. REST bootstrap bars are intentionally ignored when selecting the
    cohort, although they may remain in the historical ranking inputs.
    """
    required_symbols = max(
        1,
        safe_int(
            required_symbols,
            1,
        ),
    )

    count_by_epoch: dict[float, int] = {}
    symbols_by_epoch: dict[float, list[str]] = {}

    for raw_symbol, bars in (
        bars_by_symbol or {}
    ).items():
        symbol = str(
            raw_symbol or ""
        ).upper().strip()

        if not symbol:
            continue

        # Count each symbol no more than once for a given bucket.
        symbol_epochs = set()

        for bar in bars or []:
            bucket_start = safe_float(
                _bar_value(
                    bar,
                    "bucket_start",
                ),
                0.0,
            )

            if bucket_start > 0:
                symbol_epochs.add(
                    float(bucket_start)
                )

        for bucket_start in symbol_epochs:
            count_by_epoch[bucket_start] = (
                count_by_epoch.get(
                    bucket_start,
                    0,
                )
                + 1
            )

            symbols_by_epoch.setdefault(
                bucket_start,
                [],
            ).append(symbol)

    eligible_epochs = [
        bucket_start
        for bucket_start, symbol_count
        in count_by_epoch.items()
        if symbol_count >= required_symbols
    ]

    if not eligible_epochs:
        return {
            "status": (
                "waiting_for_completed_live_cohort"
            ),
            "required_symbol_count": (
                required_symbols
            ),
            "available_cohort_counts": {
                datetime.fromtimestamp(
                    bucket_start,
                    tz=timezone.utc,
                ).isoformat(): symbol_count
                for bucket_start, symbol_count
                in sorted(
                    count_by_epoch.items()
                )
            },
        }

    bucket_start_epoch = max(
        eligible_epochs
    )

    bucket_end_epoch = (
        bucket_start_epoch
        + max(
            1,
            safe_int(
                timeframe_seconds,
                300,
            ),
        )
    )

    cohort_symbols = sorted(
        symbols_by_epoch.get(
            bucket_start_epoch,
            [],
        )
    )

    return {
        "status": "ready",
        "bucket_start_epoch": (
            bucket_start_epoch
        ),
        "bucket_end_epoch": (
            bucket_end_epoch
        ),
        "bucket_start_timestamp": (
            datetime.fromtimestamp(
                bucket_start_epoch,
                tz=timezone.utc,
            ).isoformat()
        ),
        "bucket_end_timestamp": (
            datetime.fromtimestamp(
                bucket_end_epoch,
                tz=timezone.utc,
            ).isoformat()
        ),
        "symbol_count": len(
            cohort_symbols
        ),
        "symbols": cohort_symbols,
        "required_symbol_count": (
            required_symbols
        ),
    }


def _trim_live_inputs_to_cohort(
    bars_by_symbol: dict,
    *,
    cohort_start_epoch: float,
) -> tuple[dict, dict, dict]:
    """
    Trim every strategy input to the selected completed LIVE cohort.

    This prevents one symbol from being ranked through 09:40 while another
    symbol is only ranked through 09:35. Historical REST bootstrap bars remain
    available, but nothing newer than the chosen LIVE cohort is included.
    """
    trimmed_bars_by_symbol = {}
    trimmed_bar_counts = {}
    trimmed_prices = {}

    cohort_start_epoch = safe_float(
        cohort_start_epoch,
        0.0,
    )

    for raw_symbol, bars in (
        bars_by_symbol or {}
    ).items():
        symbol = str(
            raw_symbol or ""
        ).upper().strip()

        if not symbol:
            continue

        trimmed_bars = []

        for bar in bars or []:
            bar_epoch = (
                _bar_epoch_seconds(
                    bar
                )
            )

            if bar_epoch is None:
                continue

            if (
                bar_epoch
                <= cohort_start_epoch + 1e-6
            ):
                trimmed_bars.append(
                    bar
                )

        trimmed_bars_by_symbol[symbol] = (
            trimmed_bars
        )

        trimmed_bar_counts[symbol] = len(
            trimmed_bars
        )

        if trimmed_bars:
            trimmed_prices[symbol] = (
                safe_float(
                    _bar_value(
                        trimmed_bars[-1],
                        "close",
                    ),
                    0.0,
                )
            )
        else:
            trimmed_prices[symbol] = 0.0

    return (
        trimmed_bars_by_symbol,
        trimmed_bar_counts,
        trimmed_prices,
    )


def _opening_live_cohort_dedupe_state(
    *,
    session_date: str,
    timeframe_seconds: int,
) -> dict:
    """
    Return daily opening-shadow cohort state.

    Pending outcome rows are kept separately and are not cleared when the
    cohort-dedupe state rolls to a new session.
    """
    shadow = (
        app_state
        .setdefault(
            "layers",
            {},
        )
        .setdefault(
            "opening_live_shadow",
            {},
        )
    )

    state = shadow.setdefault(
        "cohort_dedupe",
        {},
    )

    timeframe_seconds = max(
        1,
        safe_int(
            timeframe_seconds,
            300,
        ),
    )

    if (
        state.get(
            "session_date"
        )
        != session_date
        or safe_int(
            state.get(
                "timeframe_seconds"
            ),
            0,
        )
        != timeframe_seconds
    ):
        state.clear()

        state.update({
            "session_date": (
                session_date
            ),
            "timeframe_seconds": (
                timeframe_seconds
            ),
            "last_evaluated_live_cohort_timestamp": (
                None
            ),
        })

    return state


def _live_shadow_evaluation_context(
    market_is_open: bool,
    *,
    count_live_cycle: bool,
    source_bar_timestamp=None,
) -> dict:
    """Build LIVE Layer 2 context with production's timestamp rules."""
    layers = app_state.setdefault("layers", {})
    shadow = layers.setdefault("live_strategy_shadow", {})
    state = shadow.setdefault("opening_transition", {})

    transition_cycles = safe_int(
        _execution_setting(
            "layer2_opening_transition_smoothing_cycles",
            6,
        ),
        6,
    )

    session_info = source_bar_market_session_info(
        source_bar_timestamp,
        transition_cycles=transition_cycles,
    )
    live_cycle = session_info.get("opening_transition_cycle")
    active = bool(market_is_open and session_info.get("opening_transition_active"))

    state.update({
        "date": session_info.get("session_date"),
        "source_bar_timestamp": session_info.get("source_bar_timestamp"),
        "source_bar_market_time": session_info.get("source_bar_market_time"),
        "source_phase": session_info.get("phase"),
        "source_opening_cycle": live_cycle,
        "transition_cycles": transition_cycles,
        "active": active,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    return {
        "market_is_open": bool(market_is_open),
        "opening_transition_active": active,
        "opening_transition_cycle": live_cycle if market_is_open else None,
        "opening_transition_cycles": transition_cycles,
        "opening_transition_phase": session_info.get("phase"),
        "opening_transition_source_market_time": session_info.get("source_bar_market_time"),
    }


def _stable_diagnostic_hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _strategy_parity_evidence(
    *, ranker, portfolio_builder, timeframe_seconds: int, top_n: int,
) -> dict:
    """Return auditable code/config fingerprints for REST/LIVE parity."""
    production_engine = app_state.get("layers", {}).get("engine")
    rest_ranker = getattr(production_engine, "ranker", None)
    rest_builder = getattr(production_engine, "portfolio_builder", None)
    live_layer2_config = {
        key: value for key, value in vars(portfolio_builder).items()
        if key != "previous_target_portfolio"
    }
    rest_layer2_config = {
        key: value for key, value in vars(rest_builder).items()
        if key != "previous_target_portfolio"
    } if rest_builder is not None else {}
    layer3_keys = (
        "layer3_target_hysteresis_enabled", "layer3_target_material_change",
        "layer3_target_candidate_tolerance", "layer3_target_increase_confirmation_bars",
        "layer3_target_decrease_confirmation_bars", "layer3_target_removal_confirmation_bars",
        "layer3_rolling_window_seconds", "layer3_rolling_max_trades",
        "layer3_rolling_max_buys", "layer3_rolling_max_sells",
        "layer3_rolling_max_buy_notional", "layer3_rolling_max_sell_notional",
        "layer3_rolling_max_gross_notional", "layer3_max_trade_notional_pct",
        "layer3_min_trade_notional",
    )
    layer3_config = {key: _execution_setting(key, None) for key in layer3_keys}
    rest_ranker_hash = _stable_diagnostic_hash(
        inspect.getsource(type(rest_ranker)) if rest_ranker is not None else "missing"
    )
    live_ranker_hash = _stable_diagnostic_hash(inspect.getsource(type(ranker)))
    rest_layer2_hash = _stable_diagnostic_hash(rest_layer2_config)
    live_layer2_hash = _stable_diagnostic_hash(live_layer2_config)
    return {
        "rest_ranker_code_hash": rest_ranker_hash,
        "live_ranker_code_hash": live_ranker_hash,
        "ranker_code_match": rest_ranker_hash == live_ranker_hash,
        "rest_layer2_config_hash": rest_layer2_hash,
        "live_layer2_config_hash": live_layer2_hash,
        "layer2_config_match": bool(rest_layer2_config) and rest_layer2_hash == live_layer2_hash,
        "layer3_code_hash": _stable_diagnostic_hash(inspect.getsource(build_layer3_shadow_plan)),
        "layer3_config_hash": _stable_diagnostic_hash(layer3_config),
        "simulator_config_hash": _stable_diagnostic_hash({
            "timeframe_seconds": timeframe_seconds,
            "top_n": top_n,
            "execution_price_policy": "common_current_live_price",
            "whole_share_rounding": True,
        }),
    }


def _get_live_shadow_components(
    md,
    *,
    timeframe_seconds: int,
    top_n: int,
) -> tuple[Layer1StockRanker, Layer2PortfolioBuilder]:
    shadow = app_state.setdefault("layers", {}).setdefault("live_strategy_shadow", {})

    reset_required = (
        shadow.get("timeframe_seconds") != timeframe_seconds
        or shadow.get("top_n") != top_n
        or shadow.get("ranker") is None
        or shadow.get("portfolio_builder") is None
    )

    if reset_required:
        shadow["ranker"] = Layer1StockRanker(md)
        shadow["portfolio_builder"] = Layer2PortfolioBuilder(top_n=top_n)
        shadow["timeframe_seconds"] = timeframe_seconds
        shadow["top_n"] = top_n
        shadow.setdefault("pending_outcomes", [])

        logging.info(
            "[LiveStrategyShadow] Initialized independent live-bar shadow Layer 1/2 | "
            "timeframe_seconds=%s top_n=%s",
            timeframe_seconds,
            top_n,
        )

    return shadow["ranker"], shadow["portfolio_builder"]


def _live_preference_direction(delta: float, deadband: float = 0.0025) -> str:
    if delta > deadband:
        return "LIVE_OVERWEIGHT"
    if delta < -deadband:
        return "LIVE_UNDERWEIGHT"
    return "NEUTRAL"


def _preference_sign(direction: str) -> int:
    if direction == "LIVE_OVERWEIGHT":
        return 1
    if direction == "LIVE_UNDERWEIGHT":
        return -1
    return 0


def _preference_result(score) -> str | None:
    if score is None:
        return None
    score = safe_float(score, 0.0)
    if score > 0:
        return "live_preference_helped"
    if score < 0:
        return "live_preference_hurt"
    return "neutral"


def _trade_direction_sign(decision: str | None) -> int:
    decision = str(decision or "").upper().strip()
    if decision == "BUY":
        return 1
    if decision == "SELL":
        return -1
    return 0


def _trade_result_label(score) -> str | None:
    if score is None:
        return None
    score = safe_float(score, 0.0)
    if score > 0:
        return "proposed_trade_helped"
    if score < 0:
        return "proposed_trade_hurt"
    return "neutral"


def _warmup_shadow_context() -> dict:
    """
    Return the latest non-executable warmup target and its
    restart-recovery trust classification.
    """
    diagnostics = (
        _off_hours_warmup_diagnostics()
    )

    warmup = app_state.get(
        "layers",
        {},
    ).get(
        "last_off_hours_warmup",
        {},
    )

    if not isinstance(
        warmup,
        dict,
    ):
        warmup = {}

    target = warmup.get(
        "target_portfolio",
        {},
    )

    if not isinstance(
        target,
        dict,
    ):
        target = {}

    ranked = warmup.get(
        "ranked",
        [],
    )

    if not isinstance(
        ranked,
        list,
    ):
        ranked = []

    rank_map = {}

    for index, row in enumerate(
        ranked,
        start=1,
    ):
        if not isinstance(
            row,
            dict,
        ):
            continue

        symbol = str(
            row.get("symbol")
            or ""
        ).upper().strip()

        if not symbol:
            continue

        rank_map[symbol] = {
            "rank": index,
            "score": safe_float(
                row.get("score"),
                0.0,
            ),
            "last_price": safe_float(
                row.get("last_price"),
                0.0,
            ),
            "reason": row.get(
                "reason"
            ),
        }

    return {
        "present": diagnostics.get(
            "present"
        ),
        "available": diagnostics.get(
            "available"
        ),
        "reason": diagnostics.get(
            "reason"
        ),
        "trusted_for_restart_recovery": (
            diagnostics.get(
                "trusted_for_restart_recovery"
            )
        ),
        "timestamp": diagnostics.get(
            "timestamp"
        ),
        "age_minutes": diagnostics.get(
            "age_minutes"
        ),
        "snapshot_fallback": diagnostics.get(
            "snapshot_fallback"
        ),
        "target": target,
        "rank_map": rank_map,
        "target_symbols": sorted(
            _target_symbols(
                target
            )
        ),
        "cash_pct": safe_float(
            target.get("CASH"),
            0.0,
        ),
    }


def _append_mature_opening_shadow_outcomes(*, market_is_open: bool) -> None:
    shadow = app_state.setdefault("layers", {}).setdefault("opening_live_shadow", {})
    pending = shadow.setdefault("pending_outcomes", [])

    if not pending:
        return

    md = app_state.get("market_data", {}).get("buffer")
    now_epoch = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    matured_rows = []
    keep = []

    for item in pending:
        try:
            symbol = str(item.get("symbol") or "").upper().strip()
            created_epoch = safe_float(item.get("created_epoch"), 0.0)
            start_price = safe_float(item.get("start_live_price"), 0.0)

            if not symbol or created_epoch <= 0 or start_price <= 0:
                continue

            current_price = _latest_live_price_for_symbol(md, symbol)
            age_seconds = now_epoch - created_epoch

            if current_price <= 0:
                keep.append(item)
                continue

            for seconds, key in ((600, "10m"), (1800, "30m"), (3600, "60m")):
                field = f"forward_return_{key}"
                if age_seconds >= seconds and field not in item:
                    item[field] = round((current_price - start_price) / start_price, 6)

            has_10m = "forward_return_10m" in item
            has_30m = "forward_return_30m" in item
            has_60m = "forward_return_60m" in item

            finalized_reason = None
            if has_10m and has_30m and has_60m:
                finalized_reason = "all_horizons_complete"
            elif not market_is_open and has_10m:
                finalized_reason = "market_closed_partial"

            if not finalized_reason:
                keep.append(item)
                continue

            sign = _trade_direction_sign(item.get("proposed_decision"))

            def trade_score(key: str):
                value = item.get(f"forward_return_{key}")
                if value is None or sign == 0:
                    return None
                return round(sign * safe_float(value, 0.0), 6)

            score_10m = trade_score("10m")
            score_30m = trade_score("30m")
            score_60m = trade_score("60m")

            matured_rows.append({
                "source_timestamp": item.get("source_timestamp"),
                "outcome_timestamp": now_iso,
                "source_cycle_id": item.get("cycle_id"),
                "symbol": symbol,
                "strategy_name": item.get("strategy_name"),
                "proposed_decision": item.get("proposed_decision"),
                "proposed_action": item.get("proposed_action"),
                "start_live_price": start_price,
                "outcome_live_price": current_price,
                "forward_return_10m": item.get("forward_return_10m"),
                "forward_return_30m": item.get("forward_return_30m"),
                "forward_return_60m": item.get("forward_return_60m"),
                "trade_score_10m": score_10m,
                "trade_score_30m": score_30m,
                "trade_score_60m": score_60m,
                "trade_result_10m": _trade_result_label(score_10m),
                "trade_result_30m": _trade_result_label(score_30m),
                "trade_result_60m": _trade_result_label(score_60m),
                "current_weight": item.get("current_weight"),
                "live_target_weight": item.get("live_target_weight"),
                "warmup_target_weight": item.get("warmup_target_weight"),
                "proposed_qty": item.get("proposed_qty"),
                "proposed_notional": item.get("proposed_notional"),
                "reason": item.get("reason"),
                "finalized_reason": finalized_reason,
            })

        except Exception:
            logging.warning(
                "[OpeningLiveShadow] Failed finalizing one pending outcome row.",
                exc_info=True,
            )
            keep.append(item)

    shadow["pending_outcomes"] = keep

    if matured_rows:
        append_layer_opening_shadow_outcome_rows(matured_rows)
        logging.info(
            "[OpeningLiveShadow] Appended matured outcome rows | count=%s pending=%s",
            len(matured_rows),
            len(keep),
        )


def run_opening_live_fallback_shadow(
    *,
    symbols: list[str],
    cycle_id: int | None,
    market_is_open: bool,
    rest_status: str,
    freshness_report: dict | None,
    required_fresh_symbols: int | None,
    rest_bars_by_symbol: dict | None = None,
) -> dict:
    """
    Shadow-only opening-delay diagnostic.

    This runs when production REST bars are stale, but local live bars may be
    ready. It logs what a live-only opening plan and a conservative hybrid
    opening plan would have done. It never updates the real Layer 1/2 handoff,
    never calls Layer 3, and never submits orders.
    """
    enabled = _execution_bool_setting("opening_live_shadow_enabled", True)
    _append_mature_opening_shadow_outcomes(market_is_open=market_is_open)

    if not enabled:
        return {"enabled": False, "status": "disabled"}

    if not market_is_open:
        return {"enabled": True, "status": "market_closed"}

    md = app_state.get("market_data", {}).get("buffer")
    if md is None or not hasattr(md, "get_live_bars"):
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cycle_id": cycle_id,
            "market_is_open": market_is_open,
            "rest_status": rest_status,
            "rest_fresh_count": (freshness_report or {}).get("fresh_count"),
            "required_fresh_symbols": required_fresh_symbols,
            "live_status": "missing_market_data_buffer",
            "error": "missing_market_data_buffer",
        }
        append_layer_opening_shadow_cycle_row(row)
        return row

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    now_epoch = time.time()

    timeframe_seconds = safe_int(
        _execution_setting("live_strategy_shadow_timeframe_seconds", 300),
        300,
    )
    live_bar_limit = safe_int(
        _execution_setting("live_strategy_shadow_bar_limit", 500),
        500,
    )
    min_live_bars = safe_int(
        _execution_setting("opening_live_shadow_min_bars", 6),
        6,
    )
    required_live_symbols = safe_int(
        _execution_setting("opening_live_shadow_required_symbols", required_fresh_symbols or 7),
        required_fresh_symbols or 7,
    )
    top_n = safe_int(
        _execution_setting("live_strategy_shadow_top_n", 5),
        5,
    )
    drift_threshold = safe_float(
        _execution_setting("live_strategy_shadow_min_abs_weight_drift", 0.025),
        0.025,
    )
    max_trade_notional_pct = safe_float(
        _execution_setting("opening_live_shadow_max_trade_notional_pct", 0.075),
        0.075,
    )

    rest_bars_by_symbol = rest_bars_by_symbol or {}
    live_bars_by_symbol, live_bar_counts, live_prices = _build_live_bars_by_symbol(
        symbols,
        timeframe_seconds=timeframe_seconds,
        limit=live_bar_limit,
        rest_bars_by_symbol=rest_bars_by_symbol,
        rest_bootstrap_enabled=_execution_bool_setting(
            "live_strategy_shadow_rest_bootstrap_enabled",
            True,
        ),
    )

    live_symbols_ready = [
        symbol
        for symbol, count
        in live_bar_counts.items()
        if count >= min_live_bars
    ]

    warmup = (
        _warmup_shadow_context()
    )

    live_cohort = (
        _latest_completed_live_cohort(
            live_bars_by_symbol,
            timeframe_seconds=(
                timeframe_seconds
            ),
            required_symbols=(
                required_live_symbols
            ),
        )
    )

    cohort_start_timestamp = (
        live_cohort.get(
            "bucket_start_timestamp"
        )
    )

    cohort_end_timestamp = (
        live_cohort.get(
            "bucket_end_timestamp"
        )
    )

    cycle_base = {
        "timestamp": now_iso,
        "cycle_id": cycle_id,
        "market_is_open": market_is_open,
        "rest_status": rest_status,
        "rest_fresh_count": (freshness_report or {}).get("fresh_count"),
        "required_fresh_symbols": required_fresh_symbols,
        "live_symbols_ready_count": len(live_symbols_ready),
        "live_source_bar_timestamp": (
            cohort_start_timestamp
        ),
        "live_source_bar_end_timestamp": (
            cohort_end_timestamp
        ),
        "live_source_bar_symbol_count": (
            live_cohort.get(
                "symbol_count",
                0,
            )
        ),
        "live_source_bar_symbols": (
            live_cohort.get(
                "symbols",
                [],
            )
        ),
        "live_cohort_advanced": False,
        "duplicate_live_cohort": False,
        "symbol_count": len(symbols or []),
        "warmup_present": warmup.get(
            "present"
        ),
        "warmup_available": warmup.get(
            "available"
        ),
        "warmup_reason": warmup.get(
            "reason"
        ),
        "warmup_trusted_for_restart_recovery": (
            warmup.get(
                "trusted_for_restart_recovery"
            )
        ),
        "warmup_timestamp": warmup.get(
            "timestamp"
        ),
        "warmup_age_minutes": warmup.get(
            "age_minutes"
        ),
        "warmup_snapshot_fallback": warmup.get(
            "snapshot_fallback"
        ),
        "warmup_target_symbols": warmup.get(
            "target_symbols"
        ),
    }

    if len(live_symbols_ready) < required_live_symbols:
        row = {
            **cycle_base,
            "live_status": "insufficient_live_bars",
            "live_ranked_count": 0,
            "error": f"ready={len(live_symbols_ready)} required={required_live_symbols} counts={live_bar_counts}",
        }
        append_layer_opening_shadow_cycle_row(row)
        logging.info(
            "[OpeningLiveShadow] Insufficient live bars | ready=%s required=%s counts=%s",
            len(live_symbols_ready),
            required_live_symbols,
            live_bar_counts,
        )
        return row

    if (
        live_cohort.get(
            "status"
        )
        != "ready"
    ):
        row = {
            **cycle_base,
            "live_status": (
                "waiting_for_completed_live_cohort"
            ),
            "live_ranked_count": 0,
            "error": (
                live_cohort.get(
                    "available_cohort_counts"
                )
            ),
        }

        append_layer_opening_shadow_cycle_row(
            row
        )

        logging.info(
            "[OpeningLiveShadow] Waiting for completed LIVE cohort | "
            "ready_symbols=%s required=%s cohort_counts=%s",
            len(
                live_symbols_ready
            ),
            required_live_symbols,
            live_cohort.get(
                "available_cohort_counts"
            ),
        )

        return row

    cohort_state = (
        _opening_live_cohort_dedupe_state(
            session_date=(
                now.date().isoformat()
            ),
            timeframe_seconds=(
                timeframe_seconds
            ),
        )
    )

    last_evaluated_cohort = (
        cohort_state.get(
            "last_evaluated_live_cohort_timestamp"
        )
    )

    if (
        cohort_start_timestamp
        and cohort_start_timestamp
        == last_evaluated_cohort
    ):
        row = {
            **cycle_base,
            "live_status": (
                "duplicate_live_cohort_skipped"
            ),
            "live_ranked_count": 0,
            "duplicate_live_cohort": True,
            "error": None,
        }

        append_layer_opening_shadow_cycle_row(
            row
        )

        logging.info(
            "[OpeningLiveShadow] Duplicate completed LIVE cohort skipped | "
            "cycle_id=%s cohort=%s",
            cycle_id,
            cohort_start_timestamp,
        )

        return row

    (
        live_bars_by_symbol,
        live_bar_counts,
        live_prices,
    ) = _trim_live_inputs_to_cohort(
        live_bars_by_symbol,
        cohort_start_epoch=(
            live_cohort[
                "bucket_start_epoch"
            ]
        ),
    )

    live_symbols_ready = [
        symbol
        for symbol, count
        in live_bar_counts.items()
        if count >= min_live_bars
    ]

    cycle_base.update({
        "live_symbols_ready_count": len(
            live_symbols_ready
        ),
        "live_cohort_advanced": True,
        "duplicate_live_cohort": False,
    })

    if (
        len(
            live_symbols_ready
        )
        < required_live_symbols
    ):
        row = {
            **cycle_base,
            "live_status": (
                "insufficient_bars_after_cohort_trim"
            ),
            "live_ranked_count": 0,
            "error": (
                f"ready={len(live_symbols_ready)} "
                f"required={required_live_symbols} "
                f"counts={live_bar_counts}"
            ),
        }

        append_layer_opening_shadow_cycle_row(
            row
        )

        logging.info(
            "[OpeningLiveShadow] Cohort trim left insufficient strategy history | "
            "cohort=%s ready=%s required=%s counts=%s",
            cohort_start_timestamp,
            len(
                live_symbols_ready
            ),
            required_live_symbols,
            live_bar_counts,
        )

        return row

    try:
        ranker, portfolio_builder = _get_live_shadow_components(
            md,
            timeframe_seconds=timeframe_seconds,
            top_n=top_n,
        )
        live_ranked = ranker.rank_from_bars(live_bars_by_symbol)
        live_target = portfolio_builder.build_target_portfolio(
            live_ranked,
            context=_live_shadow_evaluation_context(
                market_is_open=market_is_open,
                count_live_cycle=False,
            ),
        )
        live_status = "ok"
    except Exception as exc:
        logging.warning("[OpeningLiveShadow] Evaluation failed.", exc_info=True)
        row = {
            **cycle_base,
            "live_status": "error",
            "live_ranked_count": 0,
            "error": str(exc),
        }
        append_layer_opening_shadow_cycle_row(row)
        return row

    account = _get_account_snapshot()
    positions = _get_positions_snapshot()
    equity = safe_float(account.get("equity"), 0.0)
    cash = safe_float(account.get("cash"), 0.0)

    if equity <= 0:
        position_value = sum(
            safe_float((positions.get(symbol) or {}).get("qty"), 0.0)
            * safe_float(live_prices.get(symbol), 0.0)
            for symbol in symbols or []
        )
        equity = cash + position_value

    live_rank_map, live_top_symbols = _rank_snapshot(live_ranked)
    warmup_rank_map = warmup.get("rank_map") or {}
    warmup_target = warmup.get("target") or {}
    live_target = live_target or {}

    symbols_for_rows = sorted(
        set(str(s or "").upper().strip() for s in symbols or [] if str(s or "").strip())
        | _target_symbols(live_target)
        | _target_symbols(warmup_target)
        | set(positions.keys())
    )

    rows = []
    pending_rows = []
    live_only_counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    hybrid_counts = {"EXECUTE": 0, "DELAY": 0, "BLOCK": 0, "BUY": 0, "SELL": 0}
    total_abs_live_vs_warmup = 0.0
    max_notional = max(0.0, equity * max_trade_notional_pct)

    for symbol in symbols_for_rows:
        live_price = safe_float(live_prices.get(symbol), 0.0)
        if live_price <= 0:
            continue

        position = positions.get(symbol) or {}
        current_qty = safe_float(position.get("qty"), 0.0)
        current_value = current_qty * live_price
        current_weight = current_value / equity if equity > 0 else 0.0

        live_weight = _target_weight(live_target, symbol)
        warmup_weight = _target_weight(warmup_target, symbol)
        live_delta = live_weight - current_weight
        warmup_delta = warmup_weight - current_weight
        total_abs_live_vs_warmup += abs(live_weight - warmup_weight)

        if abs(live_delta) < drift_threshold:
            live_decision = "HOLD"
            live_reason = "live_drift_below_threshold"
        elif live_delta > 0:
            live_decision = "BUY"
            live_reason = "live_underweight_vs_target"
        else:
            live_decision = "SELL"
            live_reason = "live_overweight_or_target_removed"

        live_only_counts[live_decision] = live_only_counts.get(live_decision, 0) + 1

        requested_qty = 0
        requested_notional = 0.0
        if live_decision != "HOLD" and equity > 0:
            requested_notional = abs(live_delta) * equity
            requested_notional = min(requested_notional, max_notional) if max_notional > 0 else requested_notional
            requested_qty = math.floor(requested_notional / live_price) if live_price > 0 else 0
            if live_decision == "SELL":
                requested_qty = min(requested_qty, math.floor(current_qty))
            requested_notional = requested_qty * live_price

        warmup_same_direction = (
            live_decision == "BUY" and warmup_delta > drift_threshold
        ) or (
            live_decision == "SELL" and warmup_delta < -drift_threshold
        )

        if live_decision == "HOLD" or requested_qty <= 0:
            hybrid_action = "BLOCK"
            hybrid_decision = "HOLD"
            hybrid_reason = "no_actionable_live_trade"
            hybrid_qty = 0
            hybrid_notional = 0.0
        elif warmup_same_direction:
            hybrid_action = "EXECUTE"
            hybrid_decision = live_decision
            hybrid_reason = "live_trade_agrees_with_warmup_direction"
            hybrid_qty = requested_qty
            hybrid_notional = requested_notional
        elif live_decision == "SELL" and warmup_weight <= 0 and live_weight <= 0:
            hybrid_action = "EXECUTE"
            hybrid_decision = "SELL"
            hybrid_reason = "risk_reducing_exit_confirmed_by_live_and_warmup_absence"
            hybrid_qty = requested_qty
            hybrid_notional = requested_notional
        elif live_decision == "SELL":
            hybrid_action = "DELAY"
            hybrid_decision = "SELL"
            hybrid_reason = "live_sell_conflicts_with_warmup_target"
            hybrid_qty = 0
            hybrid_notional = 0.0
        else:
            hybrid_action = "DELAY"
            hybrid_decision = "BUY"
            hybrid_reason = "live_buy_not_confirmed_by_warmup"
            hybrid_qty = 0
            hybrid_notional = 0.0

        hybrid_counts[hybrid_action] = hybrid_counts.get(hybrid_action, 0) + 1
        if hybrid_decision in {"BUY", "SELL"}:
            hybrid_counts[hybrid_decision] = hybrid_counts.get(hybrid_decision, 0) + 1

        live_info = live_rank_map.get(symbol, {})
        warmup_info = warmup_rank_map.get(symbol, {})

        rows.append({
            "timestamp": now_iso,
            "cycle_id": cycle_id,
            "symbol": symbol,
            "market_is_open": market_is_open,
            "rest_status": rest_status,
            "rest_fresh_count": (freshness_report or {}).get("fresh_count"),
            "required_fresh_symbols": required_fresh_symbols,
            "live_status": live_status,
            "live_source_bar_timestamp": (
                cohort_start_timestamp
            ),
            "live_source_bar_end_timestamp": (
                cohort_end_timestamp
            ),
            "live_bar_count": (
                live_bar_counts.get(
                    symbol
                )
            ),
            "current_qty": current_qty,
            "current_weight": round(current_weight, 6),
            "live_target_weight": round(live_weight, 6),
            "warmup_target_weight": round(warmup_weight, 6),
            "target_delta_live_minus_current": round(live_delta, 6),
            "target_delta_warmup_minus_current": round(warmup_delta, 6),
            "live_only_decision": live_decision,
            "live_only_reason": live_reason,
            "live_only_qty": requested_qty,
            "live_only_notional": round(requested_notional, 2),
            "live_only_agrees_with_warmup": warmup_same_direction,
            "hybrid_decision": hybrid_decision,
            "hybrid_action": hybrid_action,
            "hybrid_reason": hybrid_reason,
            "hybrid_qty": hybrid_qty,
            "hybrid_notional": round(hybrid_notional, 2),
            "live_price": round(live_price, 4),
            "live_rank": live_info.get("rank"),
            "live_score": live_info.get("score"),
            "warmup_rank": warmup_info.get("rank"),
            "warmup_score": warmup_info.get("score"),
            "live_top_symbols": live_top_symbols,
            "warmup_target_symbols": warmup.get("target_symbols"),
        })

        if requested_qty > 0 and live_decision in {"BUY", "SELL"}:
            for strategy_name, action, qty, notional, reason in (
                ("LIVE_ONLY_OPEN", "EXECUTE", requested_qty, requested_notional, live_reason),
                ("HYBRID_OPEN", hybrid_action, hybrid_qty, hybrid_notional, hybrid_reason),
            ):
                if strategy_name == "HYBRID_OPEN" and action != "EXECUTE":
                    continue
                if qty <= 0 or notional <= 0:
                    continue

                pending_rows.append({
                    "created_epoch": (
                        live_cohort[
                            "bucket_end_epoch"
                        ]
                    ),
                    "source_timestamp": (
                        cohort_end_timestamp
                    ),
                    "cycle_id": cycle_id,
                    "symbol": symbol,
                    "strategy_name": strategy_name,
                    "proposed_decision": live_decision,
                    "proposed_action": action,
                    "start_live_price": live_price,
                    "current_weight": round(current_weight, 6),
                    "live_target_weight": round(live_weight, 6),
                    "warmup_target_weight": round(warmup_weight, 6),
                    "proposed_qty": qty,
                    "proposed_notional": round(notional, 2),
                    "reason": reason,
                })

    append_layer_opening_shadow_trade_rows(rows)

    shadow = app_state.setdefault("layers", {}).setdefault("opening_live_shadow", {})
    pending = shadow.setdefault("pending_outcomes", [])
    pending.extend(pending_rows)
    del pending[:-5000]

    cycle_row = {
        **cycle_base,
        "live_status": live_status,
        "live_ranked_count": len(live_ranked or []),
        "live_top_symbols": live_top_symbols,
        "live_cash_pct": live_target.get("CASH") if isinstance(live_target, dict) else None,
        "warmup_cash_pct": warmup.get("cash_pct"),
        "total_abs_live_vs_warmup_target_diff": round(total_abs_live_vs_warmup, 6),
        "live_only_buy_count": live_only_counts.get("BUY", 0),
        "live_only_sell_count": live_only_counts.get("SELL", 0),
        "live_only_hold_count": live_only_counts.get("HOLD", 0),
        "hybrid_execute_count": hybrid_counts.get("EXECUTE", 0),
        "hybrid_delay_count": hybrid_counts.get("DELAY", 0),
        "hybrid_block_count": hybrid_counts.get("BLOCK", 0),
        "hybrid_buy_count": hybrid_counts.get("BUY", 0),
        "hybrid_sell_count": hybrid_counts.get("SELL", 0),
        "error": None,
    }
    append_layer_opening_shadow_cycle_row(
        cycle_row
    )

    cohort_state.update({
        "last_evaluated_live_cohort_timestamp": (
            cohort_start_timestamp
        ),
        "last_evaluated_live_cohort_end_timestamp": (
            cohort_end_timestamp
        ),
        "last_evaluated_at": (
            now_iso
        ),
        "last_evaluated_cycle_id": (
            cycle_id
        ),
    })

    logging.info(
        "[OpeningLiveShadow] Complete | cycle_id=%s cohort=%s "
        "rest_status=%s live_status=%s live_only_buy=%s "
        "live_only_sell=%s hybrid_execute=%s hybrid_delay=%s pending=%s",
        cycle_id,
        cohort_start_timestamp,
        rest_status,
        live_status,
        live_only_counts.get("BUY", 0),
        live_only_counts.get("SELL", 0),
        hybrid_counts.get("EXECUTE", 0),
        hybrid_counts.get("DELAY", 0),
        len(pending),
    )

    return cycle_row


def _append_mature_live_strategy_shadow_outcomes(*, market_is_open: bool) -> None:
    shadow = app_state.setdefault("layers", {}).setdefault("live_strategy_shadow", {})
    pending = shadow.setdefault("pending_outcomes", [])

    if not pending:
        return

    md = app_state.get("market_data", {}).get("buffer")
    now_epoch = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    matured_rows = []
    keep = []

    for item in pending:
        try:
            symbol = str(item.get("symbol") or "").upper().strip()
            created_epoch = safe_float(item.get("created_epoch"), 0.0)
            start_price = safe_float(item.get("start_live_price"), 0.0)

            if not symbol or created_epoch <= 0 or start_price <= 0:
                continue

            current_price = _latest_live_price_for_symbol(md, symbol)
            age_seconds = now_epoch - created_epoch

            if current_price <= 0:
                keep.append(item)
                continue

            for seconds, key in ((600, "10m"), (1800, "30m"), (3600, "60m")):
                field = f"forward_return_{key}"
                if age_seconds >= seconds and field not in item:
                    item[field] = round((current_price - start_price) / start_price, 6)

            has_10m = "forward_return_10m" in item
            has_30m = "forward_return_30m" in item
            has_60m = "forward_return_60m" in item

            finalized_reason = None
            if has_10m and has_30m and has_60m:
                finalized_reason = "all_horizons_complete"
            elif not market_is_open and has_10m:
                finalized_reason = "market_closed_partial"

            if not finalized_reason:
                keep.append(item)
                continue

            direction = item.get("live_preference_direction")
            sign = _preference_sign(direction)

            def pref_score(key: str):
                value = item.get(f"forward_return_{key}")
                if value is None or sign == 0:
                    return None
                return round(sign * safe_float(value, 0.0), 6)

            score_10m = pref_score("10m")
            score_30m = pref_score("30m")
            score_60m = pref_score("60m")

            matured_rows.append({
                "source_timestamp": item.get("source_timestamp"),
                "outcome_timestamp": now_iso,
                "source_cycle_id": item.get("cycle_id"),
                "symbol": symbol,
                "market_is_open_at_source": item.get("market_is_open"),
                "rest_status": item.get("rest_status"),
                "live_status": item.get("live_status"),
                "live_timeframe_seconds": item.get("live_timeframe_seconds"),
                "rest_rank": item.get("rest_rank"),
                "live_rank": item.get("live_rank"),
                "rest_score": item.get("rest_score"),
                "live_score": item.get("live_score"),
                "current_weight": item.get("current_weight"),
                "rest_target_weight": item.get("rest_target_weight"),
                "live_target_weight": item.get("live_target_weight"),
                "target_weight_delta_live_minus_rest": item.get("target_weight_delta_live_minus_rest"),
                "rest_decision": item.get("rest_decision"),
                "live_shadow_decision": item.get("live_shadow_decision"),
                "decision_agreement": item.get("decision_agreement"),
                "live_preference_direction": direction,
                "start_live_price": start_price,
                "outcome_live_price": current_price,
                "forward_return_10m": item.get("forward_return_10m"),
                "forward_return_30m": item.get("forward_return_30m"),
                "forward_return_60m": item.get("forward_return_60m"),
                "live_preference_score_10m": score_10m,
                "live_preference_score_30m": score_30m,
                "live_preference_score_60m": score_60m,
                "live_preference_result_10m": _preference_result(score_10m),
                "live_preference_result_30m": _preference_result(score_30m),
                "live_preference_result_60m": _preference_result(score_60m),
                "finalized_reason": finalized_reason,
            })

        except Exception:
            logging.warning(
                "[LiveStrategyShadow] Failed finalizing one pending outcome row.",
                exc_info=True,
            )
            keep.append(item)

    shadow["pending_outcomes"] = keep

    if matured_rows:
        append_layer_live_strategy_outcome_rows(matured_rows)
        rebuild_rest_live_attribution_csvs()

        logging.info(
            "[LiveStrategyShadow] Appended matured outcome rows | count=%s pending=%s",
            len(matured_rows),
            len(keep),
        )


# ================================================================
# DIRECT REST-vs-LIVE SHADOW PORTFOLIO SIMULATOR
# ---------------------------------------------------------------
# Maintains independent simulated portfolios for:
#   REST = current delayed REST-bar target
#   LIVE = independent live-bar target
# This does NOT trade. It only logs simulated orders, portfolio state,
# and equity comparison rows so we can compare performance under the
# same fill/constraint assumptions.
# ================================================================


def _strategy_shadow_enabled() -> bool:
    return bool(_execution_setting("strategy_shadow_simulator_enabled", True))


def _strategy_shadow_state() -> dict:
    layers = app_state.setdefault("layers", {})
    return layers.setdefault("strategy_shadow_simulator", {})


def _strategy_shadow_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clean_strategy_shadow_target_weights(target: dict | None) -> dict[str, float]:
    if not isinstance(target, dict):
        return {}

    out = {}
    for raw_symbol, raw_weight in target.items():
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol or symbol.startswith("_") or symbol in {"CASH", "USD"}:
            continue

        weight = safe_float(raw_weight, 0.0)
        if weight > 0:
            out[symbol] = weight

    return out


def _strategy_shadow_price_map(
    *,
    symbols: list[str],
    rest_rank_map: dict,
    live_prices: dict,
    rest_bars_by_symbol: dict,
) -> dict[str, float]:
    prices = {}

    for raw_symbol in symbols or []:
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol:
            continue

        # Use one common executable mark price for both simulated portfolios.
        # Prefer the live trade/tick price when available, then fall back to REST rank/bar prices.
        price = safe_float(live_prices.get(symbol), 0.0)

        if price <= 0:
            price = safe_float((rest_rank_map.get(symbol) or {}).get("last_price"), 0.0)

        if price <= 0 and rest_bars_by_symbol.get(symbol):
            try:
                price = safe_float(rest_bars_by_symbol[symbol][-1].get("close"), 0.0)
            except Exception:
                price = 0.0

        if price > 0:
            prices[symbol] = price

    return prices


def _initial_shadow_positions_from_plan(layer3_plan: list[dict] | None) -> dict[str, float]:
    positions = {}

    for row in layer3_plan or []:
        if not isinstance(row, dict):
            continue

        symbol = str(row.get("symbol") or "").upper().strip()
        qty = safe_float(row.get("current_qty"), 0.0)

        if symbol and qty > 0:
            positions[symbol] = qty

    return positions


def _layer3_positions_snapshot_from_plan(
    layer3_plan: list[dict] | None,
) -> dict[str, dict]:
    """
    Reconstruct the common current-position snapshot from production plan rows.
    """
    positions = {}

    for row in layer3_plan or []:
        if not isinstance(row, dict):
            continue

        symbol = str(
            row.get("symbol") or ""
        ).upper().strip()
        qty = safe_float(
            row.get("current_qty"),
            0.0,
        )

        if not symbol or qty <= 0:
            continue

        price = safe_float(
            row.get("live_price"),
            0.0,
        )
        market_value = safe_float(
            row.get("current_value"),
            0.0,
        )

        if market_value <= 0 and price > 0:
            market_value = qty * price

        positions[symbol] = {
            "symbol": symbol,
            "qty": qty,
            "avg_entry_price": price,
            "current_price": price,
            "market_value": market_value,
            "unrealized_plpc": 0.0,
        }

    return positions


def _shadow_portfolio_positions_snapshot(
    portfolio: dict,
    prices: dict[str, float],
) -> dict[str, dict]:
    """
    Convert a simulated portfolio into the snapshot shape used by Layer 3.
    """
    out = {}

    for raw_symbol, raw_qty in (
        portfolio.get("positions") or {}
    ).items():
        symbol = str(
            raw_symbol or ""
        ).upper().strip()
        qty = safe_float(raw_qty, 0.0)
        price = safe_float(
            prices.get(symbol),
            0.0,
        )

        if not symbol or qty <= 0:
            continue

        out[symbol] = {
            "symbol": symbol,
            "qty": qty,
            "avg_entry_price": price,
            "current_price": price,
            "market_value": (
                qty * price
                if price > 0
                else 0.0
            ),
            "unrealized_plpc": 0.0,
        }

    return out


def _strategy_shadow_portfolio_equity(portfolio: dict, prices: dict[str, float]) -> float:
    cash = safe_float(portfolio.get("cash"), 0.0)
    positions = portfolio.setdefault("positions", {})
    equity = cash

    for symbol, qty in list(positions.items()):
        qty = safe_float(qty, 0.0)
        price = safe_float(prices.get(symbol), 0.0)

        if qty <= 0:
            positions.pop(symbol, None)
            continue

        if price > 0:
            equity += qty * price

    return equity


def _top_strategy_shadow_weights(
    portfolio: dict,
    prices: dict[str, float],
    limit: int = 5,
) -> list[str]:
    equity = _strategy_shadow_portfolio_equity(portfolio, prices)
    if equity <= 0:
        return []

    rows = []
    for symbol, qty in (portfolio.get("positions") or {}).items():
        price = safe_float(prices.get(symbol), 0.0)
        value = safe_float(qty, 0.0) * price
        if value > 0:
            rows.append((symbol, value / equity))

    rows.sort(key=lambda item: item[1], reverse=True)
    return [f"{symbol}:{weight:.4f}" for symbol, weight in rows[:limit]]


def _init_strategy_shadow_if_needed(
    *,
    layer3_plan: list[dict] | None,
    layer3_summary: dict | None,
    prices: dict[str, float],
) -> bool:
    state = _strategy_shadow_state()
    today = _strategy_shadow_today()

    if state.get("date") == today and state.get("initialized"):
        return False

    positions = _initial_shadow_positions_from_plan(layer3_plan)
    cash = safe_float((layer3_summary or {}).get("cash"), 0.0)

    # If cash is missing, approximate it from equity minus current position value.
    if cash <= 0:
        equity = safe_float((layer3_summary or {}).get("equity"), 0.0)
        position_value = sum(
            safe_float(qty, 0.0) * safe_float(prices.get(symbol), 0.0)
            for symbol, qty in positions.items()
        )
        cash = max(0.0, equity - position_value) if equity > 0 else 0.0

    portfolios = {}
    for name, source in (
        ("REST", "rest"),
        ("LIVE", "live"),
    ):
        portfolio = {
            "strategy_name": name,
            "source": source,
            "cash": float(cash),
            "positions": dict(positions),
            "cumulative_trade_count": 0,
            "cumulative_buy_notional": 0.0,
            "cumulative_sell_notional": 0.0,
            "cumulative_gross_turnover": 0.0,
        }
        equity = _strategy_shadow_portfolio_equity(portfolio, prices)
        portfolio["peak_equity"] = equity
        portfolios[name] = portfolio

    state.clear()
    state.update({
        "date": today,
        "initialized": True,
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "portfolios": portfolios,
    })

    logging.info(
        "[StrategyShadowSim] Initialized REST/LIVE shadow portfolios | date=%s cash=$%.2f positions=%s",
        today,
        cash,
        positions,
    )

    return True


def _build_strategy_shadow_trade_candidates(
    *,
    portfolio: dict,
    target: dict | None,
    prices: dict[str, float],
    equity: float,
) -> list[dict]:
    positions = portfolio.setdefault("positions", {})
    target_weights = _clean_strategy_shadow_target_weights(target)
    symbols = sorted(set(positions.keys()) | set(target_weights.keys()))

    candidates = []

    for symbol in symbols:
        price = safe_float(prices.get(symbol), 0.0)
        if price <= 0:
            continue

        current_qty = safe_float(positions.get(symbol), 0.0)
        current_value = current_qty * price
        current_weight = current_value / equity if equity > 0 else 0.0
        target_weight = safe_float(target_weights.get(symbol), 0.0)
        target_qty = (
            math.floor((target_weight * equity) / price)
            if target_weight > 0 and equity > 0
            else 0
        )
        qty_delta = target_qty - current_qty

        if abs(qty_delta) < 1:
            continue

        side = "BUY" if qty_delta > 0 else "SELL"
        requested_qty = abs(qty_delta)
        requested_notional = requested_qty * price

        candidates.append({
            "symbol": symbol,
            "decision": side,
            "side": side.lower(),
            "price": price,
            "requested_qty": requested_qty,
            "requested_notional": requested_notional,
            "current_qty": current_qty,
            "target_qty": target_qty,
            "qty_delta": qty_delta,
            "current_weight": current_weight,
            "target_weight": target_weight,
            "delta_weight": target_weight - current_weight,
            "reason": "shadow_target_rebalance",
        })

    # Sells first, then largest absolute drift/notional.
    candidates.sort(
        key=lambda row: (
            0 if row["decision"] == "SELL" else 1,
            -abs(safe_float(row.get("delta_weight"), 0.0)),
            -safe_float(row.get("requested_notional"), 0.0),
        )
    )

    return candidates


def _simulate_one_strategy_shadow_portfolio(
    *,
    portfolio: dict,
    target: dict | None,
    prices: dict[str, float],
    cycle_id: int | None,
    now_iso: str,
    planner_plan: list[dict] | None = None,
) -> tuple[list[dict], list[dict], dict]:
    max_trade_notional_pct = safe_float(
        _execution_setting("strategy_shadow_max_trade_notional_pct", 0.075),
        0.075,
    )
    max_trades_per_cycle = safe_int(
        _execution_setting("strategy_shadow_max_trades_per_cycle", 6),
        6,
    )

    target = target or {}
    target_weights = _clean_strategy_shadow_target_weights(target)
    target_cash_pct = safe_float(target.get("CASH"), 0.0) if isinstance(target, dict) else 0.0

    positions = portfolio.setdefault("positions", {})
    cash = safe_float(portfolio.get("cash"), 0.0)
    equity_before = _strategy_shadow_portfolio_equity(portfolio, prices)
    max_notional = max(0.0, equity_before * max_trade_notional_pct)

    planner_driven = planner_plan is not None

    if planner_driven:
        candidates = []

        for row in planner_plan or []:
            if not isinstance(row, dict):
                continue

            decision = str(
                row.get("decision") or ""
            ).upper().strip()
            requested_qty = safe_float(
                row.get("planned_qty"),
                0.0,
            )
            price = safe_float(
                row.get("live_price"),
                0.0,
            )

            if (
                decision not in {"BUY", "SELL"}
                or requested_qty <= 0
                or price <= 0
            ):
                continue

            candidates.append({
                "symbol": row.get("symbol"),
                "decision": decision,
                "side": decision.lower(),
                "price": price,
                "requested_qty": requested_qty,
                "requested_notional": safe_float(
                    row.get("planned_notional"),
                    0.0,
                ),
                "current_qty": row.get(
                    "current_qty"
                ),
                "target_qty": row.get(
                    "target_qty"
                ),
                "qty_delta": row.get(
                    "qty_delta"
                ),
                "current_weight": row.get(
                    "current_weight"
                ),
                "target_weight": row.get(
                    "target_weight"
                ),
                "delta_weight": row.get(
                    "delta_weight"
                ),
                "reason": row.get("reason"),
                "planner_source": row.get(
                    "planner_source"
                ),
                "target_seen_count": row.get(
                    "target_seen_count"
                ),
                "target_absent_count": row.get(
                    "target_absent_count"
                ),
            })
    else:
        candidates = (
            _build_strategy_shadow_trade_candidates(
                portfolio=portfolio,
                target=target,
                prices=prices,
                equity=equity_before,
            )
        )

    order_rows = []
    trade_count = 0
    buy_notional = 0.0
    sell_notional = 0.0

    def _current_equity() -> float:
        return cash + sum(
            safe_float(q, 0.0) * safe_float(prices.get(sym), 0.0)
            for sym, q in positions.items()
        )

    def _append_candidate_row(
        *,
        candidate: dict,
        candidate_rank: int,
        status: str,
        skip_reason: str | None,
        qty: float,
        notional: float,
        capped_qty: float,
        cash_before: float,
        cash_after: float,
        equity_before_row: float,
        equity_after_row: float,
        position_before: float,
    ) -> None:
        price = safe_float(candidate.get("price"), 0.0)
        requested_qty = safe_float(candidate.get("requested_qty"), 0.0)

        order_rows.append({
            "timestamp": now_iso,
            "cycle_id": cycle_id,
            "strategy_name": portfolio.get("strategy_name"),
            "source": portfolio.get("source"),
            "symbol": candidate.get("symbol"),
            "side": str(candidate.get("side") or candidate.get("decision") or "").lower(),
            "status": status,
            "skip_reason": skip_reason,
            "candidate_rank": candidate_rank,
            "qty": qty,
            "price": round(price, 4) if price > 0 else None,
            "notional": round(notional, 2),
            "requested_qty": requested_qty,
            "requested_notional": round(requested_qty * price, 2),
            "max_trade_notional": round(max_notional, 2),
            "capped_qty": capped_qty,
            "current_qty_before": position_before,
            "target_qty": candidate.get("target_qty"),
            "qty_delta_before": candidate.get("qty_delta"),
            "current_weight_before": round(safe_float(candidate.get("current_weight"), 0.0), 6),
            "target_weight": round(safe_float(candidate.get("target_weight"), 0.0), 6),
            "cash_before": round(cash_before, 2),
            "cash_after": round(cash_after, 2),
            "equity_before": round(equity_before_row, 2),
            "equity_after": round(equity_after_row, 2),
            "reason": candidate.get("reason"),
            "planner_source": candidate.get(
                "planner_source"
            ),
            "planner_decision": candidate.get(
                "decision"
            ),
            "planner_target_seen_count": (
                candidate.get(
                    "target_seen_count"
                )
            ),
            "planner_target_absent_count": (
                candidate.get(
                    "target_absent_count"
                )
            ),
            "planner_raw_target_weight": (
                candidate.get(
                    "raw_target_weight"
                )
            ),
            "planner_approved_target_weight": (
                candidate.get(
                    "approved_target_weight"
                )
            ),
            "planner_pending_target_weight": (
                candidate.get(
                    "pending_target_weight"
                )
            ),
            "planner_target_candidate_direction": (
                candidate.get(
                    "target_candidate_direction"
                )
            ),
            "planner_target_candidate_count": (
                candidate.get(
                    "target_candidate_count"
                )
            ),
            "planner_target_required_count": (
                candidate.get(
                    "target_required_count"
                )
            ),
            "planner_target_hysteresis_action": (
                candidate.get(
                    "target_hysteresis_action"
                )
            ),
            "planner_deferred_notional": (
                candidate.get(
                    "deferred_notional"
                )
            ),
        })

    for candidate_rank, candidate in enumerate(candidates, start=1):
        symbol = candidate["symbol"]
        side = candidate["decision"]
        price = safe_float(candidate.get("price"), 0.0)
        requested_qty = safe_float(candidate.get("requested_qty"), 0.0)
        cash_before = cash
        position_before = safe_float(positions.get(symbol), 0.0)
        equity_before_row = _current_equity()

        if price <= 0 or requested_qty <= 0:
            _append_candidate_row(
                candidate=candidate,
                candidate_rank=candidate_rank,
                status="skipped",
                skip_reason="bad_price_or_requested_qty",
                qty=0,
                notional=0.0,
                capped_qty=0,
                cash_before=cash_before,
                cash_after=cash,
                equity_before_row=equity_before_row,
                equity_after_row=equity_before_row,
                position_before=position_before,
            )
            continue

        if (
            not planner_driven
            and trade_count
            >= max_trades_per_cycle
        ):
            _append_candidate_row(
                candidate=candidate,
                candidate_rank=candidate_rank,
                status="skipped_cycle_trade_limit",
                skip_reason="max_trades_per_cycle_reached",
                qty=0,
                notional=0.0,
                capped_qty=0,
                cash_before=cash_before,
                cash_after=cash,
                equity_before_row=equity_before_row,
                equity_after_row=equity_before_row,
                position_before=position_before,
            )
            continue

        capped_qty = (
            requested_qty
            if planner_driven
            else min(
                requested_qty,
                (
                    math.floor(
                        max_notional / price
                    )
                    if max_notional > 0
                    else requested_qty
                ),
            )
        )
        qty = math.floor(capped_qty)

        if qty <= 0:
            _append_candidate_row(
                candidate=candidate,
                candidate_rank=candidate_rank,
                status="skipped_too_small",
                skip_reason="quantity_rounded_to_zero_or_below_max_trade_size",
                qty=0,
                notional=0.0,
                capped_qty=capped_qty,
                cash_before=cash_before,
                cash_after=cash,
                equity_before_row=equity_before_row,
                equity_after_row=equity_before_row,
                position_before=position_before,
            )
            continue

        if side == "SELL":
            qty = min(qty, int(position_before))
            if qty <= 0:
                _append_candidate_row(
                    candidate=candidate,
                    candidate_rank=candidate_rank,
                    status="skipped_no_position",
                    skip_reason="no_sellable_position",
                    qty=0,
                    notional=0.0,
                    capped_qty=capped_qty,
                    cash_before=cash_before,
                    cash_after=cash,
                    equity_before_row=equity_before_row,
                    equity_after_row=equity_before_row,
                    position_before=position_before,
                )
                continue

            notional = qty * price
            positions[symbol] = position_before - qty
            if positions[symbol] <= 0:
                positions.pop(symbol, None)
            cash += notional
            sell_notional += notional

        elif side == "BUY":
            cash_limited_qty = min(qty, math.floor(cash / price) if price > 0 else 0)
            if cash_limited_qty <= 0:
                _append_candidate_row(
                    candidate=candidate,
                    candidate_rank=candidate_rank,
                    status="skipped_cash",
                    skip_reason="insufficient_shadow_cash",
                    qty=0,
                    notional=0.0,
                    capped_qty=capped_qty,
                    cash_before=cash_before,
                    cash_after=cash,
                    equity_before_row=equity_before_row,
                    equity_after_row=equity_before_row,
                    position_before=position_before,
                )
                continue

            qty = cash_limited_qty
            notional = qty * price
            positions[symbol] = position_before + qty
            cash -= notional
            buy_notional += notional

        else:
            _append_candidate_row(
                candidate=candidate,
                candidate_rank=candidate_rank,
                status="skipped",
                skip_reason="unknown_side",
                qty=0,
                notional=0.0,
                capped_qty=capped_qty,
                cash_before=cash_before,
                cash_after=cash,
                equity_before_row=equity_before_row,
                equity_after_row=equity_before_row,
                position_before=position_before,
            )
            continue

        trade_count += 1
        equity_after_row = _current_equity()

        _append_candidate_row(
            candidate=candidate,
            candidate_rank=candidate_rank,
            status="executed",
            skip_reason=None,
            qty=qty,
            notional=notional,
            capped_qty=capped_qty,
            cash_before=cash_before,
            cash_after=cash,
            equity_before_row=equity_before_row,
            equity_after_row=equity_after_row,
            position_before=position_before,
        )

    portfolio["cash"] = cash
    equity_after = _strategy_shadow_portfolio_equity(portfolio, prices)

    portfolio["cumulative_trade_count"] = safe_int(portfolio.get("cumulative_trade_count", 0), 0) + trade_count
    portfolio["cumulative_buy_notional"] = safe_float(portfolio.get("cumulative_buy_notional"), 0.0) + buy_notional
    portfolio["cumulative_sell_notional"] = safe_float(portfolio.get("cumulative_sell_notional"), 0.0) + sell_notional
    portfolio["cumulative_gross_turnover"] = safe_float(portfolio.get("cumulative_gross_turnover"), 0.0) + buy_notional + sell_notional
    portfolio["peak_equity"] = max(safe_float(portfolio.get("peak_equity"), equity_after), equity_after)

    peak = safe_float(portfolio.get("peak_equity"), equity_after)
    drawdown = (equity_after - peak) / peak if peak > 0 else 0.0

    portfolio_summary = {
        "strategy_name": portfolio.get("strategy_name"),
        "source": portfolio.get("source"),
        "cash": cash,
        "equity": equity_after,
        "cash_pct": cash / equity_after if equity_after > 0 else None,
        "target_cash_pct": target_cash_pct,
        "trade_count": trade_count,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "gross_turnover": buy_notional + sell_notional,
        "cumulative_trade_count": portfolio.get("cumulative_trade_count"),
        "cumulative_buy_notional": portfolio.get("cumulative_buy_notional"),
        "cumulative_sell_notional": portfolio.get("cumulative_sell_notional"),
        "cumulative_gross_turnover": portfolio.get("cumulative_gross_turnover"),
        "peak_equity": peak,
        "drawdown_pct": drawdown,
    }

    portfolio_rows = []
    symbols = sorted(set(positions.keys()) | set(target_weights.keys()))

    for symbol in symbols:
        price = safe_float(prices.get(symbol), 0.0)
        qty = safe_float(positions.get(symbol), 0.0)
        value = qty * price if price > 0 else 0.0
        weight = value / equity_after if equity_after > 0 else 0.0
        target_weight = safe_float(target_weights.get(symbol), 0.0)

        portfolio_rows.append({
            "timestamp": now_iso,
            "cycle_id": cycle_id,
            "strategy_name": portfolio.get("strategy_name"),
            "source": portfolio.get("source"),
            "symbol": symbol,
            "qty": qty,
            "price": round(price, 4) if price > 0 else None,
            "market_value": round(value, 2),
            "weight": round(weight, 6),
            "target_weight": round(target_weight, 6),
            "weight_drift": round(weight - target_weight, 6),
            "cash": round(cash, 2),
            "equity": round(equity_after, 2),
            "cash_pct": round(cash / equity_after, 6) if equity_after > 0 else None,
            "target_cash_pct": target_cash_pct,
            "trade_count": trade_count,
            "buy_notional": round(buy_notional, 2),
            "sell_notional": round(sell_notional, 2),
            "gross_turnover": round(buy_notional + sell_notional, 2),
            "cumulative_trade_count": portfolio.get("cumulative_trade_count"),
            "cumulative_buy_notional": round(safe_float(portfolio.get("cumulative_buy_notional"), 0.0), 2),
            "cumulative_sell_notional": round(safe_float(portfolio.get("cumulative_sell_notional"), 0.0), 2),
            "cumulative_gross_turnover": round(safe_float(portfolio.get("cumulative_gross_turnover"), 0.0), 2),
            "peak_equity": round(peak, 2),
            "drawdown_pct": round(drawdown, 6),
            "target_summary": target_summary_for_log(target),
        })

    # Add a CASH row so cash exposure is visible without reconstructing it.
    portfolio_rows.append({
        "timestamp": now_iso,
        "cycle_id": cycle_id,
        "strategy_name": portfolio.get("strategy_name"),
        "source": portfolio.get("source"),
        "symbol": "CASH",
        "qty": cash,
        "price": 1.0,
        "market_value": round(cash, 2),
        "weight": round(cash / equity_after, 6) if equity_after > 0 else None,
        "target_weight": target_cash_pct,
        "weight_drift": round((cash / equity_after) - target_cash_pct, 6) if equity_after > 0 else None,
        "cash": round(cash, 2),
        "equity": round(equity_after, 2),
        "cash_pct": round(cash / equity_after, 6) if equity_after > 0 else None,
        "target_cash_pct": target_cash_pct,
        "trade_count": trade_count,
        "buy_notional": round(buy_notional, 2),
        "sell_notional": round(sell_notional, 2),
        "gross_turnover": round(buy_notional + sell_notional, 2),
        "cumulative_trade_count": portfolio.get("cumulative_trade_count"),
        "cumulative_buy_notional": round(safe_float(portfolio.get("cumulative_buy_notional"), 0.0), 2),
        "cumulative_sell_notional": round(safe_float(portfolio.get("cumulative_sell_notional"), 0.0), 2),
        "cumulative_gross_turnover": round(safe_float(portfolio.get("cumulative_gross_turnover"), 0.0), 2),
        "peak_equity": round(peak, 2),
        "drawdown_pct": round(drawdown, 6),
        "target_summary": target_summary_for_log(target),
    })

    return order_rows, portfolio_rows, portfolio_summary


def _strategy_shadow_comparison_winner(summaries: dict[str, dict]) -> str | None:
    if not summaries:
        return None

    valid = {
        name: safe_float(summary.get("equity"), 0.0)
        for name, summary in summaries.items()
        if safe_float(summary.get("equity"), 0.0) > 0
    }

    if not valid:
        return None

    return max(valid.items(), key=lambda item: item[1])[0]


def run_strategy_shadow_portfolio_simulation(
    *,
    symbols: list[str],
    cycle_id: int | None,
    market_is_open: bool,
    rest_status: str,
    live_status: str,
    rest_target: dict | None,
    live_target: dict | None,
    rest_rank_map: dict,
    live_prices: dict,
    rest_bars_by_symbol: dict,
    live_bar_counts: dict | None,
    rest_source_bar_timestamp: (
        str | None
    ),
    live_source_bar_timestamp: (
        str | None
    ),
    layer3_plan: list[dict] | None,
    layer3_summary: dict | None,
) -> dict:
    if not _strategy_shadow_enabled():
        return {"status": "disabled"}

    now_iso = datetime.now(timezone.utc).isoformat()

    if not market_is_open or rest_status != "ok" or live_status != "ok":
        row = {
            "timestamp": now_iso,
            "cycle_id": cycle_id,
            "status": "skipped",
            "market_is_open": market_is_open,
            "rest_status": rest_status,
            "live_status": live_status,
            "error": "requires_market_open_and_ok_rest_live",
        }
        append_layer_strategy_shadow_comparison_row(row)
        return row

    prices = _strategy_shadow_price_map(
        symbols=symbols,
        rest_rank_map=rest_rank_map,
        live_prices=live_prices,
        rest_bars_by_symbol=rest_bars_by_symbol,
    )

    if not prices:
        row = {
            "timestamp": now_iso,
            "cycle_id": cycle_id,
            "status": "skipped",
            "market_is_open": market_is_open,
            "rest_status": rest_status,
            "live_status": live_status,
            "error": "no_prices_available",
        }
        append_layer_strategy_shadow_comparison_row(row)
        return row

    _init_strategy_shadow_if_needed(
        layer3_plan=layer3_plan,
        layer3_summary=layer3_summary,
        prices=prices,
    )

    portfolios = _strategy_shadow_state().setdefault("portfolios", {})
    target_by_strategy = {
        "REST": rest_target,
        "LIVE": live_target,
    }

    all_order_rows = []
    all_portfolio_rows = []
    summaries = {}

    for strategy_name in ("REST", "LIVE"):
        portfolio = portfolios.get(strategy_name)
        if not isinstance(portfolio, dict):
            continue

        portfolio_equity = (
            _strategy_shadow_portfolio_equity(
                portfolio,
                prices,
            )
        )

        positions_snapshot = (
            _shadow_portfolio_positions_snapshot(
                portfolio,
                prices,
            )
        )

        account_snapshot = {
            "source": (
                f"{strategy_name.lower()}_"
                "shadow_portfolio"
            ),
            "broker_snapshot_ok": True,
            "account_snapshot_error": None,
            "equity": portfolio_equity,
            "cash": safe_float(
                portfolio.get("cash"),
                0.0,
            ),
            "buying_power": safe_float(
                portfolio.get("cash"),
                0.0,
            ),
        }

        source_bar_counts = (
            {
                symbol: len(
                    rest_bars_by_symbol.get(
                        symbol,
                        [],
                    ) or []
                )
                for symbol in symbols
            }
            if strategy_name == "REST"
            else dict(live_bar_counts or {})
        )

        rest_warmup_target = (
            app_state.get("layers", {})
            .get("last_off_hours_warmup", {})
            .get("target_portfolio", {})
        )

        live_warmup_target = (
            app_state.get("layers", {})
            .get("live_strategy_shadow", {})
            .get("last_off_hours_target", {})
        )

        bootstrap_symbols = _target_symbols(
            (
                rest_warmup_target
                if strategy_name == "REST"
                else live_warmup_target
            )
        )

        planner_result = build_layer3_shadow_plan(
            planner_source=(
                f"{strategy_name}_SIM"
            ),
            target=(
                target_by_strategy.get(
                    strategy_name
                ) or {}
            ),
            account=account_snapshot,
            positions=positions_snapshot,
            ranked_prices=prices,
            planner_state=portfolio.setdefault(
                "layer3_planner_state",
                {},
            ),
            market_is_open=market_is_open,
            cycle_id=safe_int(cycle_id, 0),
            bar_counts=source_bar_counts,
            bootstrap_eligible_symbols=(
                bootstrap_symbols
            ),
            open_order_symbols=set(),
            open_order_details={},
            fail_safe_active=False,
            last_trade_prices=prices,
            source_bar_timestamp=(
                rest_source_bar_timestamp
                if strategy_name == "REST"
                else live_source_bar_timestamp
            ),
        )

        planner_plan = planner_result.get(
            "plan",
            [],
        )

        (
            order_rows,
            portfolio_rows,
            summary,
        ) = _simulate_one_strategy_shadow_portfolio(
            portfolio=portfolio,
            target=target_by_strategy.get(
                strategy_name
            ),
            prices=prices,
            cycle_id=cycle_id,
            now_iso=now_iso,
            planner_plan=planner_plan,
        )

        summary["planner_summary"] = (
            planner_result.get("summary", {})
        )
        all_order_rows.extend(order_rows)
        all_portfolio_rows.extend(portfolio_rows)
        summaries[strategy_name] = summary

    append_layer_strategy_shadow_order_rows(all_order_rows)
    append_layer_strategy_shadow_portfolio_rows(all_portfolio_rows)

    def value(name: str, key: str, default=0.0):
        return safe_float((summaries.get(name) or {}).get(key), default)

    comparison_row = {
        "timestamp": now_iso,
        "cycle_id": cycle_id,
        "status": "ok",
        "market_is_open": market_is_open,
        "rest_status": rest_status,
        "live_status": live_status,
        "rest_equity": round(value("REST", "equity"), 2),
        "live_equity": round(value("LIVE", "equity"), 2),
        "live_minus_rest_equity": round(value("LIVE", "equity") - value("REST", "equity"), 2),
        "rest_cash_pct": round(value("REST", "cash_pct"), 6),
        "live_cash_pct": round(value("LIVE", "cash_pct"), 6),
        "rest_cycle_gross_turnover": round(value("REST", "gross_turnover"), 2),
        "live_cycle_gross_turnover": round(value("LIVE", "gross_turnover"), 2),
        "rest_cumulative_gross_turnover": round(value("REST", "cumulative_gross_turnover"), 2),
        "live_cumulative_gross_turnover": round(value("LIVE", "cumulative_gross_turnover"), 2),
        "rest_drawdown_pct": round(value("REST", "drawdown_pct"), 6),
        "live_drawdown_pct": round(value("LIVE", "drawdown_pct"), 6),
        "rest_planner_status": (
            (summaries.get("REST") or {})
            .get("planner_summary", {})
            .get("status")
        ),
        "live_planner_status": (
            (summaries.get("LIVE") or {})
            .get("planner_summary", {})
            .get("status")
        ),
        "rest_planner_decision_counts": (
            (summaries.get("REST") or {})
            .get("planner_summary", {})
            .get("decision_counts")
        ),
        "live_planner_decision_counts": (
            (summaries.get("LIVE") or {})
            .get("planner_summary", {})
            .get("decision_counts")
        ),
        "rest_planner_rolling_trade_limits": (
            (summaries.get("REST") or {})
            .get("planner_summary", {})
            .get("rolling_trade_limits")
        ),
        "live_planner_rolling_trade_limits": (
            (summaries.get("LIVE") or {})
            .get("planner_summary", {})
            .get("rolling_trade_limits")
        ),
        "rest_planner_target_hysteresis": (
            (summaries.get("REST") or {})
            .get("planner_summary", {})
            .get("target_hysteresis")
        ),
        "live_planner_target_hysteresis": (
            (summaries.get("LIVE") or {})
            .get("planner_summary", {})
            .get("target_hysteresis")
        ),
        "winner_by_equity": _strategy_shadow_comparison_winner(summaries),
        "live_better_than_rest": value("LIVE", "equity") > value("REST", "equity"),
        "rest_top_weights": _top_strategy_shadow_weights(portfolios.get("REST", {}), prices),
        "live_top_weights": _top_strategy_shadow_weights(portfolios.get("LIVE", {}), prices),
        "error": None,
    }

    append_layer_strategy_shadow_comparison_row(comparison_row)

    logging.info(
        "[StrategyShadowSim] Complete | cycle_id=%s winner=%s REST=$%.2f LIVE=$%.2f live_minus_rest=$%.2f",
        cycle_id,
        comparison_row.get("winner_by_equity"),
        comparison_row.get("rest_equity"),
        comparison_row.get("live_equity"),
        comparison_row.get("live_minus_rest_equity"),
    )

    return comparison_row


def run_live_strategy_shadow_comparison(
    *,
    symbols: list[str],
    cycle_id: int | None,
    market_is_open: bool,
    rest_ranked: list | None,
    rest_target: dict | None,
    rest_status: str,
    layer3_plan: list[dict] | None = None,
    layer3_summary: dict | None = None,
    rest_bars_by_symbol: dict | None = None,
    allow_closed_market: bool = False,
    record_outcomes: bool = True,
) -> dict:
    """
    Run an independent live-bar Layer 1/2 shadow evaluation and log comparison CSVs.

    This is intentionally side-effect isolated from the real Layer 1/2/3 path:
    - does not update app_state["layers"]["latest"]
    - does not call run_layer3_dry_run()
    - does not submit orders
    """
    enabled = bool(_execution_setting("live_strategy_shadow_enabled", True))
    _append_mature_opening_shadow_outcomes(market_is_open=market_is_open)

    if not enabled:
        _append_mature_live_strategy_shadow_outcomes(market_is_open=market_is_open)
        return {"enabled": enabled, "status": "disabled"}

    if not market_is_open and not allow_closed_market:
        _append_mature_live_strategy_shadow_outcomes(market_is_open=market_is_open)
        return {"enabled": enabled, "status": "market_closed"}

    md = app_state.get("market_data", {}).get("buffer")
    if md is None or not hasattr(md, "get_live_bars"):
        return {"enabled": enabled, "status": "missing_market_data_buffer"}

    _append_mature_live_strategy_shadow_outcomes(market_is_open=market_is_open)

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    now_epoch = time.time()

    timeframe_seconds = safe_int(
        _execution_setting("live_strategy_shadow_timeframe_seconds", 300),
        300,
    )
    live_bar_limit = safe_int(
        _execution_setting("live_strategy_shadow_bar_limit", 500),
        500,
    )
    min_live_bars = safe_int(
        _execution_setting("live_strategy_shadow_min_bars", 61),
        61,
    )
    top_n = safe_int(
        _execution_setting("live_strategy_shadow_top_n", 5),
        5,
    )
    drift_threshold = safe_float(
        _execution_setting("live_strategy_shadow_min_abs_weight_drift", 0.025),
        0.025,
    )

    rest_bars_by_symbol = rest_bars_by_symbol or {}
    layer3_plan = layer3_plan or []
    layer3_summary = (
        layer3_summary or {}
    )

    rest_bootstrap_enabled = _execution_bool_setting(
        "live_strategy_shadow_rest_bootstrap_enabled",
        True,
    )

    live_bars_by_symbol, live_bar_counts, live_prices = _build_live_bars_by_symbol(
        symbols,
        timeframe_seconds=timeframe_seconds,
        limit=live_bar_limit,
        rest_bars_by_symbol=rest_bars_by_symbol,
        rest_bootstrap_enabled=rest_bootstrap_enabled,
    )

    rest_source_bar_timestamp = (
        layer3_summary.get(
            "source_bar_timestamp"
        )
        or _latest_source_bar_timestamp(
            rest_bars_by_symbol,
            symbols,
        )
    )

    live_source_bar_timestamp = (
        _latest_source_bar_timestamp(
            live_bars_by_symbol,
            symbols,
        )
    )

    # Bootstrap history can make a stale symbol look numerically "ready".
    # Require one completed local cohort shared by the entire production
    # universe and trim all inputs to that exact timestamp.
    live_cohort = _latest_completed_live_cohort(
        live_bars_by_symbol,
        timeframe_seconds=timeframe_seconds,
        required_symbols=max(1, len(symbols or [])),
    )
    if live_cohort.get("status") == "ready":
        live_bars_by_symbol, live_bar_counts, live_prices = _trim_live_inputs_to_cohort(
            live_bars_by_symbol,
            cohort_start_epoch=live_cohort.get("bucket_start_epoch"),
        )
        live_source_bar_timestamp = live_cohort.get("bucket_start_timestamp")

    live_symbols_ready = [
        symbol
        for symbol, count in live_bar_counts.items()
        if count >= min_live_bars
    ]

    symbol_count = len(symbols or [])
    rest_ranked_count = len(rest_ranked or [])

    cycle_base = {
        "timestamp": now_iso,
        "cycle_id": cycle_id,
        "market_is_open": market_is_open,
        "rest_status": rest_status,
        "live_timeframe_seconds": timeframe_seconds,
        "live_min_required_bars": min_live_bars,
        "live_symbols_ready": live_symbols_ready,
        "symbol_count": symbol_count,
        "rest_ranked_count": rest_ranked_count,
        "live_cohort_status": live_cohort.get("status"),
        "live_cohort_timestamp": live_cohort.get("bucket_start_timestamp"),
        "live_cohort_symbol_count": live_cohort.get("symbol_count", 0),
        "live_cohort_symbols": live_cohort.get("symbols", []),
    }

    # True comparison rule:
    # The live shadow should only evaluate when the REST path produced a real
    # comparable Layer 1/2 target. If REST skipped, do not update live smoothing
    # state and do not run the portfolio simulator.
    comparable_rest_statuses = {"ok", "market_closed_warmup"}

    if rest_status not in comparable_rest_statuses or rest_ranked_count <= 0:
        row = {
            **cycle_base,
            "live_status": "skipped_no_comparable_rest_target",
            "live_ranked_count": 0,
            "error": f"rest_status={rest_status} rest_ranked_count={rest_ranked_count}",
        }
        append_layer_live_strategy_shadow_cycle_row(row)
        logging.info(
            "[LiveStrategyShadow] Skipped because REST path did not produce a comparable target | "
            "rest_status=%s rest_ranked=%s",
            rest_status,
            rest_ranked_count,
        )
        return row

    if live_cohort.get("status") != "ready":
        row = {
            **cycle_base,
            "live_status": "incomplete_live_cohort",
            "live_ranked_count": 0,
            "error": json.dumps(live_cohort, sort_keys=True, default=str),
        }
        append_layer_live_strategy_shadow_cycle_row(row)
        return row

    # True comparison rule:
    # Every symbol that REST evaluated must also have enough completed local
    # 5-minute live bars. This prevents LIVE from ranking 1 symbol while REST
    # ranks the full universe.
    if len(live_symbols_ready) < symbol_count:
        row = {
            **cycle_base,
            "live_status": "insufficient_live_bars",
            "live_ranked_count": 0,
            "error": (
                f"requires_all_symbols_ready ready={len(live_symbols_ready)}/"
                f"{symbol_count} counts={live_bar_counts}"
            ),
        }
        append_layer_live_strategy_shadow_cycle_row(row)
        logging.info(
            "[LiveStrategyShadow] Insufficient completed 5-minute live bars | "
            "ready=%s/%s min_bars=%s timeframe=%ss counts=%s",
            len(live_symbols_ready),
            symbol_count,
            min_live_bars,
            timeframe_seconds,
            live_bar_counts,
        )
        return row

    try:
        ranker, portfolio_builder = _get_live_shadow_components(
            md,
            timeframe_seconds=timeframe_seconds,
            top_n=top_n,
        )

        live_ranked = ranker.rank_from_bars(live_bars_by_symbol)

        # True comparison rule:
        # If REST ranked N symbols, LIVE must rank N symbols before we build
        # a target. This avoids updating the live Layer 2 smoothing state with
        # a partial universe.
        if len(live_ranked or []) < rest_ranked_count:
            row = {
                **cycle_base,
                "live_status": "insufficient_live_ranked_symbols",
                "live_ranked_count": len(live_ranked or []),
                "error": (
                    f"live_ranked_count={len(live_ranked or [])} "
                    f"rest_ranked_count={rest_ranked_count}"
                ),
            }
            append_layer_live_strategy_shadow_cycle_row(row)
            logging.info(
                "[LiveStrategyShadow] Insufficient live ranked symbols | "
                "live_ranked=%s rest_ranked=%s",
                len(live_ranked or []),
                rest_ranked_count,
            )
            return row

        live_target = portfolio_builder.build_target_portfolio(
            live_ranked,
            context=_live_shadow_evaluation_context(
                market_is_open=market_is_open,
                count_live_cycle=market_is_open,
                source_bar_timestamp=live_source_bar_timestamp,
            ),
        )

        parity_evidence = _strategy_parity_evidence(
            ranker=ranker,
            portfolio_builder=portfolio_builder,
            timeframe_seconds=timeframe_seconds,
            top_n=top_n,
        )

        if not market_is_open:
            app_state.setdefault(
                "layers",
                {},
            ).setdefault(
                "live_strategy_shadow",
                {},
            )["last_off_hours_target"] = dict(
                live_target or {}
            )

        live_status = "ok"

    except Exception as exc:
        logging.warning("[LiveStrategyShadow] Evaluation failed.", exc_info=True)
        row = {
            **cycle_base,
            "live_status": "error",
            "live_ranked_count": 0,
            "error": str(exc),
        }
        append_layer_live_strategy_shadow_cycle_row(row)
        return row

    rest_rank_map, rest_top_symbols = _rank_snapshot(rest_ranked)
    live_rank_map, live_top_symbols = _rank_snapshot(live_ranked)

    rest_target = rest_target or {}
    live_target = live_target or {}
    rest_meta = _target_meta(rest_target)
    live_meta = _target_meta(live_target)

    plan_by_symbol = {
        str(row.get("symbol") or "").upper().strip(): row
        for row in layer3_plan
        if isinstance(row, dict) and row.get("symbol")
    }

    equity = safe_float(layer3_summary.get("equity"), 0.0)

    live_layer3_result = {
        "plan": [],
        "summary": {
            "status": "not_estimated",
            "reason": (
                "missing_common_account_snapshot"
            ),
        },
    }

    if equity > 0:
        common_positions = (
            _layer3_positions_snapshot_from_plan(
                layer3_plan
            )
        )

        common_account = {
            "source": (
                "common_broker_snapshot_"
                "from_rest_plan"
            ),
            "broker_snapshot_ok": True,
            "account_snapshot_error": None,
            "equity": equity,
            "cash": safe_float(
                layer3_summary.get("cash"),
                0.0,
            ),
            "buying_power": safe_float(
                layer3_summary.get(
                    "buying_power"
                ),
                safe_float(
                    layer3_summary.get("cash"),
                    0.0,
                ),
            ),
        }

        common_open_order_symbols = {
            str(
                row.get("symbol") or ""
            ).upper().strip()
            for row in layer3_plan
            if (
                isinstance(row, dict)
                and row.get(
                    "open_order_exists"
                )
            )
        }

        common_open_order_details = {
            str(
                row.get("symbol") or ""
            ).upper().strip(): row.get(
                "open_order_detail"
            )
            for row in layer3_plan
            if (
                isinstance(row, dict)
                and row.get(
                    "open_order_exists"
                )
            )
        }

        shadow_state = (
            app_state.setdefault(
                "layers",
                {},
            ).setdefault(
                "live_strategy_shadow",
                {},
            )
        )

        live_layer3_result = (
            build_layer3_shadow_plan(
                planner_source="LIVE_COMMON",
                target=live_target,
                account=common_account,
                positions=common_positions,
                ranked_prices=live_prices,
                planner_state=(
                    shadow_state.setdefault(
                        "layer3_common_"
                        "planner_state",
                        {},
                    )
                ),
                market_is_open=market_is_open,
                cycle_id=safe_int(
                    cycle_id,
                    0,
                ),
                bar_counts=live_bar_counts,
                bootstrap_eligible_symbols=(
                    _target_symbols(
                        shadow_state.get(
                            "last_off_hours_target",
                            {},
                        )
                    )
                ),
                open_order_symbols=(
                    common_open_order_symbols
                ),
                open_order_details=(
                    common_open_order_details
                ),
                fail_safe_active=bool(
                    layer3_summary.get(
                        "fail_safe_active"
                    )
                ),
                last_trade_prices=live_prices,
                source_bar_timestamp=(
                    live_source_bar_timestamp
                ),
            )
        )

    live_plan = live_layer3_result.get(
        "plan",
        [],
    )

    live_plan_by_symbol = {
        str(
            row.get("symbol") or ""
        ).upper().strip(): row
        for row in live_plan
        if (
            isinstance(row, dict)
            and row.get("symbol")
        )
    }

    live_planner_summary = (
        live_layer3_result.get(
            "summary",
            {},
        )
    )

    symbols_for_rows = sorted(
        set(str(s or "").upper().strip() for s in symbols or [] if str(s or "").strip())
        | _target_symbols(rest_target)
        | _target_symbols(live_target)
        | set(plan_by_symbol.keys())
        | set(live_plan_by_symbol.keys())
    )

    rows = []
    pending_rows = []
    agreement_values = []
    total_abs_target_diff = 0.0
    max_abs_target_diff = 0.0
    rest_decision_counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    live_decision_counts = {"BUY": 0, "SELL": 0, "HOLD": 0, "NOT_ESTIMATED": 0}

    for symbol in symbols_for_rows:
        plan_row = plan_by_symbol.get(symbol, {})
        rest_info = rest_rank_map.get(symbol, {})
        live_info = live_rank_map.get(symbol, {})

        rest_rank = rest_info.get("rank")
        live_rank = live_info.get("rank")

        rest_score = rest_info.get("score")
        live_score = live_info.get("score")

        rest_weight = _target_weight(rest_target, symbol)
        live_weight = _target_weight(live_target, symbol)
        target_delta = live_weight - rest_weight
        abs_target_delta = abs(target_delta)
        total_abs_target_diff += abs_target_delta
        max_abs_target_diff = max(max_abs_target_diff, abs_target_delta)

        live_plan_row = (
            live_plan_by_symbol.get(
                symbol,
                {},
            )
        )

        current_weight = (
            safe_float(
                plan_row.get("current_weight"),
                0.0,
            )
            if plan_row
            else (
                safe_float(
                    live_plan_row.get(
                        "current_weight"
                    ),
                    0.0,
                )
                if live_plan_row
                else None
            )
        )

        rest_decision = (
            str(
                plan_row.get(
                    "decision"
                ) or "HOLD"
            ).upper().strip()
            if plan_row
            else "NOT_ESTIMATED"
        )

        if rest_decision in rest_decision_counts:
            rest_decision_counts[
                rest_decision
            ] += 1

        live_decision = (
            str(
                live_plan_row.get(
                    "decision"
                ) or "HOLD"
            ).upper().strip()
            if live_plan_row
            else "NOT_ESTIMATED"
        )

        live_decision_counts[live_decision] = (
            live_decision_counts.get(
                live_decision,
                0,
            ) + 1
        )

        decision_agreement = (
            rest_decision == live_decision
            if rest_decision != "NOT_ESTIMATED" and live_decision != "NOT_ESTIMATED"
            else None
        )
        if decision_agreement is not None:
            agreement_values.append(bool(decision_agreement))

        rest_price = safe_float(rest_info.get("last_price"), 0.0)
        if rest_price <= 0 and rest_bars_by_symbol.get(symbol):
            rest_price = safe_float(rest_bars_by_symbol[symbol][-1].get("close"), 0.0)

        live_price = safe_float(live_prices.get(symbol), 0.0)
        live_vs_rest_price_pct = (
            round((live_price - rest_price) / rest_price, 6)
            if live_price > 0 and rest_price > 0
            else None
        )

        rank_delta = (
            live_rank - rest_rank
            if isinstance(live_rank, int) and isinstance(rest_rank, int)
            else None
        )

        score_delta = (
            round(safe_float(live_score, 0.0) - safe_float(rest_score, 0.0), 6)
            if live_score is not None and rest_score is not None
            else None
        )

        preference_direction = _live_preference_direction(target_delta)
        live_estimated_notional = (
            safe_float(
                live_plan_row.get(
                    "planned_notional"
                ),
                0.0,
            )
            if live_plan_row
            else None
        )

        row = {
            "timestamp": now_iso,
            "cycle_id": cycle_id,
            "symbol": symbol,
            "market_is_open": market_is_open,
            "rest_status": rest_status,
            "live_status": live_status,
            "live_timeframe_seconds": timeframe_seconds,
            "live_bar_count": live_bar_counts.get(symbol),
            "rest_bar_count": len(rest_bars_by_symbol.get(symbol, []) or []),
            "rest_rank": rest_rank,
            "live_rank": live_rank,
            "rank_delta_live_minus_rest": rank_delta,
            "rest_score": rest_score,
            "live_score": live_score,
            "score_delta_live_minus_rest": score_delta,
            "current_weight": current_weight,
            "rest_target_weight": rest_weight,
            "live_target_weight": live_weight,
            "target_weight_delta_live_minus_rest": round(target_delta, 6),
            "rest_decision": rest_decision,
            "live_shadow_decision": live_decision,
            "decision_agreement": decision_agreement,
            "live_preference_direction": preference_direction,
            "rest_planned_qty": plan_row.get("planned_qty") if plan_row else None,
            "rest_planned_notional": plan_row.get("planned_notional") if plan_row else None,
            "live_shadow_estimated_notional": live_estimated_notional,
            "live_planner_source": (
                live_plan_row.get(
                    "planner_source"
                )
                if live_plan_row
                else None
            ),
            "live_planner_status": (
                live_planner_summary.get(
                    "status"
                )
            ),
            "live_planner_planned_qty": (
                live_plan_row.get(
                    "planned_qty"
                )
                if live_plan_row
                else None
            ),
            "live_planner_target_seen_count": (
                live_plan_row.get(
                    "target_seen_count"
                )
                if live_plan_row
                else None
            ),
            "live_planner_target_absent_count": (
                live_plan_row.get(
                    "target_absent_count"
                )
                if live_plan_row
                else None
            ),
            "live_planner_raw_target_weight": (
                live_plan_row.get(
                    "raw_target_weight"
                )
                if live_plan_row
                else None
            ),
            "live_planner_approved_target_weight": (
                live_plan_row.get(
                    "approved_target_weight"
                )
                if live_plan_row
                else None
            ),
            "live_planner_pending_target_weight": (
                live_plan_row.get(
                    "pending_target_weight"
                )
                if live_plan_row
                else None
            ),
            "live_planner_target_candidate_direction": (
                live_plan_row.get(
                    "target_candidate_direction"
                )
                if live_plan_row
                else None
            ),
            "live_planner_target_candidate_count": (
                live_plan_row.get(
                    "target_candidate_count"
                )
                if live_plan_row
                else None
            ),
            "live_planner_target_required_count": (
                live_plan_row.get(
                    "target_required_count"
                )
                if live_plan_row
                else None
            ),
            "live_planner_target_hysteresis_action": (
                live_plan_row.get(
                    "target_hysteresis_action"
                )
                if live_plan_row
                else None
            ),
            "live_planner_deferred_notional": (
                live_plan_row.get(
                    "deferred_notional"
                )
                if live_plan_row
                else None
            ),
            "rest_price": rest_price if rest_price > 0 else None,
            "live_price": live_price if live_price > 0 else None,
            "live_vs_rest_price_pct": live_vs_rest_price_pct,
            "rest_reason": plan_row.get("reason") if plan_row else None,
            "live_reason": (
                live_plan_row.get("reason")
                if live_plan_row
                else None
            ),
            "rest_top_symbols": rest_top_symbols,
            "live_top_symbols": live_top_symbols,
            "rest_target_summary": target_summary_for_log(rest_target),
            "live_target_summary": target_summary_for_log(live_target),
        }
        rows.append(row)

        if record_outcomes and market_is_open and live_price > 0:
            pending_rows.append({
                "created_epoch": now_epoch,
                "source_timestamp": now_iso,
                "cycle_id": cycle_id,
                "symbol": symbol,
                "market_is_open": market_is_open,
                "rest_status": rest_status,
                "live_status": live_status,
                "live_timeframe_seconds": timeframe_seconds,
                "rest_rank": rest_rank,
                "live_rank": live_rank,
                "rest_score": rest_score,
                "live_score": live_score,
                "current_weight": current_weight,
                "rest_target_weight": rest_weight,
                "live_target_weight": live_weight,
                "target_weight_delta_live_minus_rest": round(target_delta, 6),
                "rest_decision": rest_decision,
                "live_shadow_decision": live_decision,
                "decision_agreement": decision_agreement,
                "live_preference_direction": preference_direction,
                "start_live_price": live_price,
            })

    append_layer_live_strategy_shadow_rows(rows)

    shadow_state = app_state.setdefault("layers", {}).setdefault("live_strategy_shadow", {})
    pending = shadow_state.setdefault("pending_outcomes", [])
    pending.extend(pending_rows)
    del pending[:-5000]

    rest_top_set = set(rest_top_symbols)
    live_top_set = set(live_top_symbols)
    overlap_symbols = sorted(rest_top_set & live_top_set)
    row_count = max(len(symbols_for_rows), 1)
    decision_agreement_rate = (
        round(sum(1 for v in agreement_values if v) / len(agreement_values), 4)
        if agreement_values else None
    )

    cycle_row = {
        **cycle_base,
        "live_status": live_status,
        "live_ranked_count": len(live_ranked or []),
        "rest_top_symbols": rest_top_symbols,
        "live_top_symbols": live_top_symbols,
        "top5_overlap_count": len(overlap_symbols),
        "top5_overlap_symbols": overlap_symbols,
        "total_abs_target_weight_diff": round(total_abs_target_diff, 6),
        "avg_abs_target_weight_diff": round(total_abs_target_diff / row_count, 6),
        "max_abs_target_weight_diff": round(max_abs_target_diff, 6),
        "decision_agreement_rate": decision_agreement_rate,
        "rest_buy_count": rest_decision_counts.get("BUY", 0),
        "rest_sell_count": rest_decision_counts.get("SELL", 0),
        "rest_hold_count": rest_decision_counts.get("HOLD", 0),
        "live_buy_count": live_decision_counts.get("BUY", 0),
        "live_sell_count": live_decision_counts.get("SELL", 0),
        "live_hold_count": live_decision_counts.get("HOLD", 0),
        "live_not_estimated_count": live_decision_counts.get("NOT_ESTIMATED", 0),
        "live_cash_pct": live_target.get("CASH") if isinstance(live_target, dict) else None,
        "rest_cash_pct": rest_target.get("CASH") if isinstance(rest_target, dict) else None,
        "live_market_strength": live_meta.get("market_strength"),
        "rest_market_strength": rest_meta.get("market_strength"),
        "live_planner_status": (
            live_planner_summary.get(
                "status"
            )
        ),
        "live_planner_decision_counts": (
            live_planner_summary.get(
                "decision_counts"
            )
        ),
        "live_planner_bootstrap_confirmation_applied": (
            live_planner_summary.get(
                "bootstrap_confirmation_applied"
            )
        ),
        "live_planner_rolling_trade_limits": (
            live_planner_summary.get(
                "rolling_trade_limits"
            )
        ),
        "live_planner_target_hysteresis": (
            live_planner_summary.get(
                "target_hysteresis"
            )
        ),
        **parity_evidence,
        "strategy_parity_verified": bool(
            parity_evidence.get("ranker_code_match")
            and parity_evidence.get("layer2_config_match")
        ),
        "error": None,
    }

    append_layer_live_strategy_shadow_cycle_row(cycle_row)

    try:
        run_strategy_shadow_portfolio_simulation(
            symbols=symbols_for_rows,
            cycle_id=cycle_id,
            market_is_open=market_is_open,
            rest_status=rest_status,
            live_status=live_status,
            rest_target=rest_target,
            live_target=live_target,
            rest_rank_map=rest_rank_map,
            live_prices=live_prices,
            rest_bars_by_symbol=rest_bars_by_symbol,
            live_bar_counts=live_bar_counts,
            rest_source_bar_timestamp=(
                rest_source_bar_timestamp
            ),
            live_source_bar_timestamp=(
                live_source_bar_timestamp
            ),
            layer3_plan=layer3_plan,
            layer3_summary=layer3_summary,
        )
    except Exception:
        logging.warning(
            "[StrategyShadowSim] Failed during live strategy shadow comparison | cycle_id=%s",
            cycle_id,
            exc_info=True,
        )

    rebuild_rest_live_attribution_csvs()

    logging.info(
        "[LiveStrategyShadow] Complete | cycle_id=%s live_status=%s live_ranked=%s "
        "rest_ranked=%s top5_overlap=%s target_diff_total=%.4f decision_agreement=%s pending_outcomes=%s",
        cycle_id,
        live_status,
        len(live_ranked or []),
        len(rest_ranked or []),
        len(overlap_symbols),
        total_abs_target_diff,
        decision_agreement_rate,
        len(pending),
    )

    return cycle_row


async def _sleep_with_shutdown(
    shutdown_event,
    seconds: float,
) -> bool:
    deadline = (
        time.monotonic()
        + max(0.0, float(seconds))
    )

    while not shutdown_event.is_set():
        remaining = deadline - time.monotonic()

        if remaining <= 0:
            return False

        await asyncio.sleep(
            min(0.5, remaining)
        )

    return True


def _rest_bar_gate_state() -> dict:
    return (
        app_state.setdefault("layers", {})
        .setdefault("bar_gates", {})
        .setdefault("REST", {})
    )


async def _wait_for_distinct_fresh_rest_bars(
    *,
    symbols: list[str],
    required_fresh_symbols: int,
    market_is_open_at_start: bool,
    next_layer3_cycle_id: int | None,
) -> dict:
    """
    Poll REST bars until a fresh, distinct five-minute cohort is available.

    Market-open waits continue until:
    - a new cohort arrives
    - shutdown is requested
    - the market closes

    Off hours perform one check only.
    """
    shutdown_event = (
        app_state["stream"]["shutdown_event"]
    )
    gate_state = _rest_bar_gate_state()

    poll_seconds = max(
        5.0,
        safe_float(
            _execution_setting(
                "layer_monitor_rest_bar_poll_seconds",
                15.0,
            ),
            15.0,
        ),
    )

    log_seconds = max(
        poll_seconds,
        safe_float(
            _execution_setting(
                "layer_monitor_rest_bar_wait_log_seconds",
                60.0,
            ),
            60.0,
        ),
    )

    freshness_market_hours_only = (
        _execution_bool_setting(
            "bar_freshness_market_hours_only",
            True,
        )
    )

    max_age_minutes = safe_float(
        _execution_setting(
            "bar_freshness_max_age_minutes",
            35.0,
        ),
        35.0,
    )

    started = time.monotonic()
    next_info_log_at = 0.0
    attempts = 0

    gate_state["waiting"] = True
    gate_state["wait_started_at"] = (
        datetime.now(timezone.utc).isoformat()
    )

    while not shutdown_event.is_set():
        attempts += 1

        market_is_open_now = (
            get_market_is_open(app_state)
        )

        freshness_required = bool(
            market_is_open_now
            or not freshness_market_hours_only
        )

        bars_by_symbol = (
            fetch_recent_bars_with_min_count(
                app_state.get(
                    "stock_data_client"
                ),
                symbols,
                min_bars=180,
                timeframe_minutes=5,
                initial_lookback_hours=96,
                max_lookback_hours=336,
                min_ready_symbols=(
                    required_fresh_symbols
                ),
            )
        )

        (
            freshness_probe,
            freshness_report,
        ) = filter_fresh_bars(
            bars_by_symbol,
            symbols,
            max_age_minutes=max_age_minutes,
        )

        fresh_bars_by_symbol = (
            freshness_probe
            if freshness_required
            else bars_by_symbol
        )

        freshness_report.update({
            "freshness_required": (
                freshness_required
            ),
            "market_is_open": (
                market_is_open_now
            ),
            "required_fresh_symbols": (
                required_fresh_symbols
            ),
        })

        gate_report = build_distinct_bar_report(
            source="REST",
            bars_by_symbol=(
                fresh_bars_by_symbol
            ),
            symbols=symbols,
            last_accepted_timestamp_by_symbol=(
                gate_state.get(
                    "last_accepted_timestamp_by_symbol",
                    {},
                )
            ),
            required_new_symbols=(
                required_fresh_symbols
            ),
        )

        freshness_ready = bool(
            not freshness_required
            or freshness_report.get(
                "fresh_count",
                0,
            )
            >= required_fresh_symbols
        )

        ready = bool(
            freshness_ready
            and gate_report.get("ready")
        )

        elapsed = round(
            time.monotonic() - started,
            3,
        )

        gate_report.update({
            "status": (
                "ready"
                if ready
                else "waiting"
            ),
            "attempts": attempts,
            "wait_seconds": elapsed,
            "poll_seconds": poll_seconds,
            "freshness_ready": (
                freshness_ready
            ),
            "fresh_count": (
                freshness_report.get(
                    "fresh_count",
                    0,
                )
            ),
            "required_fresh_symbols": (
                required_fresh_symbols
            ),
            "market_is_open": (
                market_is_open_now
            ),
        })

        app_state.setdefault(
            "layers",
            {},
        )["bar_freshness"] = (
            freshness_report
        )

        gate_state["last_poll_at"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        if not ready:
            fallback_reason = (
                "market_open_rest_snapshot_fallback"
                if market_is_open_now
                else
                "market_closed_rest_snapshot_fallback"
            )

            warmup_fallback = (
                _try_store_rest_snapshot_warmup(
                    symbols=symbols,
                    bars_by_symbol=(
                        bars_by_symbol
                    ),
                    freshness_report=(
                        freshness_report
                    ),
                    required_symbols=(
                        required_fresh_symbols
                    ),
                    reason=(
                        fallback_reason
                    ),
                    rest_bar_gate=(
                        gate_report
                    ),
                )
            )

            gate_report.update({
                "warmup_fallback_status": (
                    warmup_fallback.get(
                        "status"
                    )
                ),
                "warmup_fallback_stored": bool(
                    warmup_fallback.get(
                        "stored"
                    )
                ),
                "warmup_fallback_reason": (
                    fallback_reason
                ),
            })

        gate_state["last_poll_report"] = (
            dict(gate_report)
        )

        if ready:
            gate_state["waiting"] = False
            gate_state["last_ready_report"] = (
                dict(gate_report)
            )

            logging.info(
                "[RestBarGate] Ready | "
                "candidate=%s new=%s/%s "
                "attempts=%s wait_seconds=%.1f "
                "fresh=%s/%s",
                gate_report.get(
                    "candidate_bar_timestamp"
                ),
                gate_report.get(
                    "new_symbol_count"
                ),
                gate_report.get(
                    "required_new_symbols"
                ),
                attempts,
                elapsed,
                freshness_report.get(
                    "fresh_count",
                    0,
                ),
                required_fresh_symbols,
            )

            return {
                "status": "ready",
                "market_is_open": (
                    market_is_open_now
                ),
                "bars_by_symbol": (
                    bars_by_symbol
                ),
                "fresh_bars_by_symbol": (
                    fresh_bars_by_symbol
                ),
                "freshness_report": (
                    freshness_report
                ),
                "gate_report": gate_report,
            }

        logging.debug(
            "[RestBarGate] "
            "Duplicate/not-ready | report=%s",
            gate_report,
        )

        if elapsed >= next_info_log_at:
            logging.info(
                "[RestBarGate] Waiting | "
                "candidate=%s new=%s/%s "
                "fresh=%s/%s attempt=%s "
                "wait_seconds=%.1f lagging=%s "
                "duplicate=%s",
                gate_report.get(
                    "candidate_bar_timestamp"
                ),
                gate_report.get(
                    "new_symbol_count"
                ),
                gate_report.get(
                    "required_new_symbols"
                ),
                freshness_report.get(
                    "fresh_count",
                    0,
                ),
                required_fresh_symbols,
                attempts,
                elapsed,
                gate_report.get(
                    "lagging_symbols"
                ),
                gate_report.get(
                    "duplicate_symbols"
                ),
            )

            next_info_log_at = (
                elapsed + log_seconds
            )

            # Keep the opening-delay diagnostic
            # running while production waits.
            if market_is_open_now:
                run_opening_live_fallback_shadow(
                    symbols=symbols,
                    cycle_id=(
                        next_layer3_cycle_id
                    ),
                    market_is_open=True,
                    rest_status=(
                        "waiting_for_distinct_"
                        "fresh_rest_bar"
                    ),
                    freshness_report=(
                        freshness_report
                    ),
                    required_fresh_symbols=(
                        required_fresh_symbols
                    ),
                    rest_bars_by_symbol=(
                        bars_by_symbol
                    ),
                )

        # Do not poll continuously overnight.
        if not market_is_open_now:
            gate_state["waiting"] = False

            status = (
                "market_closed_while_waiting"
                if market_is_open_at_start
                else "no_new_bar_off_hours"
            )

            gate_report["status"] = status

            return {
                "status": status,
                "market_is_open": False,
                "bars_by_symbol": (
                    bars_by_symbol
                ),
                "fresh_bars_by_symbol": (
                    fresh_bars_by_symbol
                ),
                "freshness_report": (
                    freshness_report
                ),
                "gate_report": gate_report,
            }

        shutdown_requested = (
            await _sleep_with_shutdown(
                shutdown_event,
                poll_seconds,
            )
        )

        if shutdown_requested:
            break

    gate_state["waiting"] = False

    return {
        "status": "shutdown",
        "market_is_open": (
            get_market_is_open(app_state)
        ),
        "bars_by_symbol": {},
        "fresh_bars_by_symbol": {},
        "freshness_report": {},
        "gate_report": {
            "status": "shutdown",
            "attempts": attempts,
            "wait_seconds": round(
                time.monotonic() - started,
                3,
            ),
        },
    }


async def run_layer_monitor(
    interval_seconds: int = 300,
) -> None:
    """
    Runs Layer 1/2 evaluation on a timer.

    Layer 1/2 builds the target portfolio.
    Layer 3 builds a rebalance plan.

    Order execution is controlled by LAYER3_EXECUTION_ENABLED.
    When disabled, Layer 3 remains dry-run only.
    """
    interval_seconds = (
        normalize_layer_interval_seconds(
            safe_int(
                _execution_setting(
                    "layer_monitor_interval_seconds",
                    interval_seconds,
                ),
                interval_seconds,
            )
        )
    )

    logging.info(
        "[Layers] Layer monitor started | "
        "interval_seconds=%s "
        "wall_clock_aligned=True "
        "distinct_rest_bar_gate=True",
        interval_seconds,
    )

    while not app_state["stream"]["shutdown_event"].is_set():
        try:
            layer_engine = app_state.get("layers", {}).get("engine")

            if not layer_engine:
                logging.info("[Layers] Engine not initialized yet. Skipping.")
            else:
                symbols = app_state.get("main", {}).get("symbol", [])

                if not symbols:
                    logging.info("[Layers] No symbols configured. Skipping.")
                else:
                    logging.info("[Layers] Starting scheduled evaluation.")

                    market_is_open = get_market_is_open(app_state)

                    run_24_7 = (
                        _execution_bool_setting(
                            "layer_monitor_run_24_7",
                            True,
                        )
                    )

                    if not run_24_7 and not market_is_open:
                        logging.info(
                            "[Layers] Market closed and layer_monitor_run_24_7=false. "
                            "Skipping this cycle."
                        )

                        append_layer_cycle_row(
                            status="skipped",
                            reason="market_closed_run_24_7_false",
                            market_is_open=market_is_open,
                            ranked_count=0,
                        )

                        continue

                    logging.info(
                        "[Layers] Market status | market_is_open=%s run_24_7=%s",
                        market_is_open,
                        run_24_7,
                    )

                    md = app_state.get("market_data", {}).get("buffer")
                    if md:
                        tick_counts = {
                            symbol: len(md.get_recent_prices(symbol))
                            for symbol in symbols
                        }
                        logging.info("[Layers] Tick counts: %s", tick_counts)

                    required_fresh_symbols = (
                        _fresh_symbol_requirement(
                            len(symbols)
                        )
                    )

                    next_layer3_cycle_id = int(
                        app_state.get(
                            "layers",
                            {},
                        )
                        .get(
                            "rebalance",
                            {},
                        )
                        .get(
                            "last_cycle_id",
                            0,
                        )
                        or 0
                    ) + 1

                    bar_gate_result = await (
                        _wait_for_distinct_fresh_rest_bars(
                            symbols=symbols,
                            required_fresh_symbols=(
                                required_fresh_symbols
                            ),
                            market_is_open_at_start=(
                                market_is_open
                            ),
                            next_layer3_cycle_id=(
                                next_layer3_cycle_id
                                if market_is_open
                                else None
                            ),
                        )
                    )

                    if (
                        bar_gate_result.get("status")
                        != "ready"
                    ):
                        logging.info(
                            "[RestBarGate] "
                            "No strategic cycle run | "
                            "status=%s report=%s",
                            bar_gate_result.get(
                                "status"
                            ),
                            bar_gate_result.get(
                                "gate_report"
                            ),
                        )
                        continue

                    market_is_open = bool(
                        bar_gate_result.get(
                            "market_is_open"
                        )
                    )

                    bars_by_symbol = (
                        bar_gate_result.get(
                            "bars_by_symbol",
                            {},
                        )
                    )

                    fresh_bars_by_symbol = (
                        bar_gate_result.get(
                            "fresh_bars_by_symbol",
                            {},
                        )
                    )

                    freshness_report = (
                        bar_gate_result.get(
                            "freshness_report",
                            {},
                        )
                    )

                    rest_bar_gate_report = (
                        bar_gate_result.get(
                            "gate_report",
                            {},
                        )
                    )

                    if market_is_open:
                        logging.info(
                            "[Layers] "
                            "Symbols being evaluated: %s",
                            symbols,
                        )
                    else:
                        logging.info(
                            "[Layers] "
                            "Symbols being observed: %s",
                            symbols,
                        )

                    bar_counts = {
                        symbol: len(
                            bars_by_symbol.get(
                                symbol,
                                [],
                            )
                        )
                        for symbol in symbols
                    }

                    logging.info(
                        "[Layers] Bar counts: %s",
                        bar_counts,
                    )

                    symbols_with_60_bars = [
                        symbol
                        for symbol, count
                        in bar_counts.items()
                        if count >= 60
                    ]

                    symbols_with_180_bars = [
                        symbol
                        for symbol, count
                        in bar_counts.items()
                        if count >= 180
                    ]

                    logging.info(
                        "[Layers] Bar readiness | "
                        ">=60 bars=%s/%s "
                        ">=180 bars=%s/%s",
                        len(symbols_with_60_bars),
                        len(symbols),
                        len(symbols_with_180_bars),
                        len(symbols),
                    )

                    app_state.setdefault(
                        "layers",
                        {},
                    )["bar_freshness"] = (
                        freshness_report
                    )

                    live_bar_health_summary = (
                        _append_live_bar_health_snapshot(
                            symbols=symbols,
                            rest_bars_by_symbol=(
                                bars_by_symbol
                            ),
                            market_is_open=(
                                market_is_open
                            ),
                            cycle_id=(
                                next_layer3_cycle_id
                                if market_is_open
                                else None
                            ),
                        )
                    )

                    logging.info(
                        "[Bars] Accepted distinct "
                        "REST cohort | candidate=%s "
                        "new=%s/%s fresh=%s/%s "
                        "ages=%s",
                        rest_bar_gate_report.get(
                            "candidate_bar_timestamp"
                        ),
                        rest_bar_gate_report.get(
                            "new_symbol_count"
                        ),
                        rest_bar_gate_report.get(
                            "required_new_symbols"
                        ),
                        freshness_report.get(
                            "fresh_count",
                            0,
                        ),
                        required_fresh_symbols,
                        freshness_report.get(
                            "latest_bar_ages_minutes",
                            {},
                        ),
                    )

                    evaluation_symbols = list(
                        fresh_bars_by_symbol.keys()
                    )

                    rest_source_bar_timestamp = (
                        rest_bar_gate_report.get(
                            "candidate_bar_timestamp"
                        )
                    )

                    if not evaluation_symbols:
                        logging.warning(
                            "[Layers] Skipping Layer 1/2/3/4 because no evaluation symbols are available."
                        )

                        append_layer_cycle_row(
                            status="skipped",
                            reason="no_evaluation_symbols",
                            market_is_open=market_is_open,
                            fresh_count=freshness_report.get("fresh_count") if isinstance(freshness_report, dict) else None,
                            required_fresh_symbols=required_fresh_symbols,
                            live_bar_summary=(
                                live_bar_health_summary
                            ),
                            ranked_count=0,
                        )

                        continue

                    if not market_is_open:
                        result = layer_engine.evaluate(
                            evaluation_symbols,
                            bars_by_symbol=fresh_bars_by_symbol,
                            context=_layer2_evaluation_context(
                                market_is_open=False,
                                count_live_cycle=False,
                                source_bar_timestamp=(
                                    rest_source_bar_timestamp
                                ),
                            ),
                        )

                        ranked = result.get("ranked", [])
                        target = result.get("target_portfolio", {})

                        fresh_bar_counts = {
                            symbol: len(fresh_bars_by_symbol.get(symbol, []))
                            for symbol in evaluation_symbols
                        }

                        store_off_hours_layer_warmup_result(
                            symbols=(
                                evaluation_symbols
                            ),
                            bar_counts=(
                                fresh_bar_counts
                            ),
                            ranked=ranked,
                            target=target,
                            freshness_report=(
                                freshness_report
                            ),
                            reason=(
                                "market_closed_target_warmup"
                            ),
                            rest_bar_gate=(
                                rest_bar_gate_report
                            ),
                        )

                        run_live_strategy_shadow_comparison(
                            symbols=evaluation_symbols,
                            cycle_id=None,
                            market_is_open=False,
                            rest_ranked=ranked,
                            rest_target=target,
                            rest_status="market_closed_warmup",
                            layer3_plan=[],
                            layer3_summary={},
                            rest_bars_by_symbol=fresh_bars_by_symbol,
                            allow_closed_market=True,
                            record_outcomes=False,
                        )

                        append_layer_cycle_row(
                            status="warmup_only",
                            reason="market_closed_target_warmup",
                            market_is_open=market_is_open,
                            fresh_count=freshness_report.get("fresh_count") if isinstance(freshness_report, dict) else None,
                            required_fresh_symbols=required_fresh_symbols,
                            rest_bar_gate=(
                                rest_bar_gate_report
                            ),
                            live_bar_summary=(
                                live_bar_health_summary
                            ),
                            ranked_count=len(ranked or []),
                            top_symbols=[
                                getattr(r, "symbol", None)
                                for r in (ranked or [])[:5]
                            ],
                            target_summary=target_summary_for_log(target),
                            layer3_summary={},
                            layer4_result={},
                        )

                        logging.info(
                            "[Layers] Market closed; completed Layer 1/2 target warmup. "
                            "Skipping Layer 3 planning and Layer 4 execution."
                        )

                        _expire_active_plan_for_market_close()
                        _append_mature_live_strategy_shadow_outcomes(market_is_open=False)

                        accept_distinct_bar_report(
                            _rest_bar_gate_state(),
                            rest_bar_gate_report,
                            cycle_id=None,
                            accepted_reason=(
                                "market_closed_"
                                "target_warmup"
                            ),
                        )

                        continue

                    restart_recovery = (
                        _restart_recovery_context(
                            market_is_open=True,
                            source_bar_timestamp=(
                                rest_source_bar_timestamp
                            ),
                        )
                    )

                    result = layer_engine.evaluate(
                        evaluation_symbols,
                        bars_by_symbol=fresh_bars_by_symbol,
                        context=_layer2_evaluation_context(
                            market_is_open=True,
                            count_live_cycle=True,
                            source_bar_timestamp=(
                                rest_source_bar_timestamp
                            ),
                        ),
                    )

                    logging.info("[Layers] Evaluation complete.")

                    ranked = result.get("ranked", [])
                    target = result.get("target_portfolio", {})

                    logging.info(
                        "[Layers] Ranked count=%s target_summary=%s",
                        len(ranked),
                        target_summary_for_log(target),
                    )

                    fresh_bar_counts = {
                        symbol: len(fresh_bars_by_symbol.get(symbol, []))
                        for symbol in evaluation_symbols
                    }

                    next_layer3_cycle_id = int(
                        app_state.get("layers", {})
                        .get("rebalance", {})
                        .get("last_cycle_id", 0)
                        or 0
                    ) + 1

                    store_latest_layer_result(
                        symbols=evaluation_symbols,
                        bar_counts=fresh_bar_counts,
                        ranked=ranked,
                        target=target,
                    )

                    layer3_result = await asyncio.to_thread(
                        run_layer3_dry_run,
                        source_bar_timestamp=rest_source_bar_timestamp,
                        restart_recovery=restart_recovery,
                    )

                    if isinstance(layer3_result, dict) and (
                        "plan" in layer3_result or "summary" in layer3_result
                    ):
                        layer3_plan = layer3_result.get("plan", [])
                        layer3_summary = layer3_result.get("summary", {})
                    else:
                        # Backward-compatible fallback:
                        # If run_layer3_dry_run() still returns only a summary,
                        # read the plan/summary from app_state["layers"]["rebalance"].
                        rebalance = app_state.get("layers", {}).get("rebalance", {})
                        layer3_plan = rebalance.get("last_plan", [])
                        layer3_summary = (
                            layer3_result
                            if isinstance(
                                layer3_result,
                                dict,
                            )
                            else rebalance.get(
                                "last_summary",
                                {},
                            )
                        )

                    if (
                        layer3_summary.get(
                            "status"
                        )
                        == "ok"
                    ):
                        committed_recovery = (
                            _restart_recovery_context(
                                market_is_open=True,
                                source_bar_timestamp=(
                                    rest_source_bar_timestamp
                                ),
                                advance=True,
                            )
                        )

                        layer3_summary.update({
                            "restart_recovery_warmup_present": (
                                committed_recovery.get(
                                    "warmup_present"
                                )
                            ),
                            "restart_recovery_warmup_reason": (
                                committed_recovery.get(
                                    "warmup_reason"
                                )
                            ),
                            "restart_recovery_warmup_trusted": (
                                committed_recovery.get(
                                    "warmup_trusted_for_restart_recovery"
                                )
                            ),
                            "restart_recovery_warmup_timestamp": (
                                committed_recovery.get(
                                    "warmup_timestamp"
                                )
                            ),
                            "restart_recovery_warmup_age_minutes": (
                                committed_recovery.get(
                                    "warmup_age_minutes"
                                )
                            ),
                            "restart_recovery_warmup_snapshot_fallback": (
                                committed_recovery.get(
                                    "warmup_snapshot_fallback"
                                )
                            ),
                            "restart_recovery_observed_bars": (
                                committed_recovery.get(
                                    "observed_bars"
                                )
                            ),
                            "restart_recovery_source_timestamps": (
                                committed_recovery.get(
                                    "source_timestamps"
                                )
                            ),
                            "restart_recovery_ready_after_source_bar": (
                                committed_recovery.get(
                                    "ready_after_source_bar"
                                )
                            ),
                            "restart_recovery_completed": bool(
                                committed_recovery.get(
                                    "completed"
                                )
                            ),
                            "restart_recovery_evidence_committed": bool(
                                committed_recovery.get(
                                    "evidence_committed"
                                )
                            ),
                        })

                    logging.info(
                        "[Layer3] Plan summary | cycle_id=%s plan_id=%s status=%s decisions=%s "
                        "equity=$%s cash=$%s target_symbols=%s target_cash_pct=%s "
                        "open_orders=%s fail_safe_active=%s opening_transition=%s "
                        "source_open_cycle=%s observed_open_cycles=%s "
                        "restart_recovery=%s recovery_bars=%s/%s execution_blocked=%s "
                        "warmup_stale=%s warmup_skipped=%s",
                        layer3_summary.get("cycle_id"),
                        layer3_summary.get("plan_id"),
                        layer3_summary.get("status"),
                        layer3_summary.get("decision_counts"),
                        layer3_summary.get("equity"),
                        layer3_summary.get("cash"),
                        layer3_summary.get("target_symbol_count"),
                        layer3_summary.get("target_cash_pct"),
                        layer3_summary.get("open_order_count"),
                        layer3_summary.get("fail_safe_active"),
                        layer3_summary.get("opening_transition_active"),
                        layer3_summary.get("opening_transition_cycle"),
                        layer3_summary.get("open_session_live_cycle_count"),
                        layer3_summary.get("restart_recovery_required"),
                        layer3_summary.get("restart_recovery_observed_bars"),
                        layer3_summary.get("restart_recovery_required_bars"),
                        layer3_summary.get("restart_recovery_execution_blocked"),
                        layer3_summary.get("bootstrap_confirmation_warmup_stale_symbols"),
                        layer3_summary.get("bootstrap_confirmation_warmup_skipped_symbols"),
                    )

                    layer3_summary[
                        "rest_bar_gate"
                    ] = dict(
                        rest_bar_gate_report
                    )

                    # Layer 3 completed its strategic
                    # plan for this bar. Mark it accepted
                    # before diagnostics and execution so
                    # a downstream error cannot plan the
                    # same bar twice.
                    accept_distinct_bar_report(
                        _rest_bar_gate_state(),
                        rest_bar_gate_report,
                        cycle_id=(
                            layer3_summary.get(
                                "cycle_id"
                            )
                        ),
                        accepted_reason=(
                            "market_open_"
                            "strategic_cycle"
                        ),
                    )

                    layer4_execution_result = await asyncio.to_thread(
                        execute_layer4_plan,
                        layer3_plan,
                        layer3_summary,
                    )

                    # All strategy comparisons are diagnostic-only and run
                    # after broker execution so they cannot delay live orders.
                    try:
                        await asyncio.to_thread(
                            run_live_strategy_shadow_comparison,
                            symbols=evaluation_symbols,
                            cycle_id=next_layer3_cycle_id,
                            market_is_open=market_is_open,
                            rest_ranked=ranked,
                            rest_target=target,
                            rest_status="ok",
                            layer3_plan=layer3_plan,
                            layer3_summary=layer3_summary,
                            rest_bars_by_symbol=fresh_bars_by_symbol,
                        )
                    except Exception:
                        logging.warning(
                            "[LiveStrategyShadow] Post-execution comparison failed.",
                            exc_info=True,
                        )

                    # Research runs only after production execution so its
                    # simulation and CSV writes cannot delay broker orders.
                    try:
                        await asyncio.to_thread(
                            run_research_strategy_shadow,
                            ranked=ranked,
                            production_target=target,
                            bars_by_symbol=fresh_bars_by_symbol,
                            layer3_plan=layer3_plan,
                            layer3_summary=layer3_summary,
                            source_bar_timestamp=rest_source_bar_timestamp,
                        )
                    except Exception:
                        logging.warning(
                            "[ResearchStrategyShadow] Evaluation failed; production is unchanged.",
                            exc_info=True,
                        )

                    append_layer_cycle_row(
                        status="ok",
                        reason=None,
                        market_is_open=market_is_open,
                        fresh_count=freshness_report.get("fresh_count") if isinstance(freshness_report, dict) else None,
                        required_fresh_symbols=required_fresh_symbols,
                        ranked_count=len(ranked or []),
                        top_symbols=[
                            getattr(r, "symbol", None)
                            for r in (ranked or [])[:5]
                        ],
                        target_summary=target_summary_for_log(target),
                        layer3_summary=layer3_summary,
                        layer4_result=layer4_execution_result,
                        rest_bar_gate=(
                            rest_bar_gate_report
                        ),
                        live_bar_summary=(
                            live_bar_health_summary
                        ),
                    )

                    logging.info(
                        "[Layers] Layer4 handoff complete | cycle_id=%s plan_id=%s attempted=%s "
                        "submitted=%s skipped=%s errors=%s blocked_reason=%s count_integrity_ok=%s order_count=%s",
                        layer4_execution_result.get("cycle_id"),
                        layer4_execution_result.get("plan_id"),
                        layer4_execution_result.get("attempted"),
                        layer4_execution_result.get("submitted"),
                        layer4_execution_result.get("skipped"),
                        layer4_execution_result.get("errors"),
                        layer4_execution_result.get("blocked_reason"),
                        layer4_execution_result.get("count_integrity_ok"),
                        len(layer4_execution_result.get("orders", []) or []),
                    )

                    if not ranked:
                        logging.info(
                            "[Layers] No ranked symbols yet. Likely not enough market data. "
                            "This is normal during startup/off-hours."
                        )
                    else:
                        top_symbols = [
                            f"{r.symbol}:{r.score:.4f}"
                            for r in ranked[:5]
                        ]

                        logging.info(
                            "[Layers] Evaluation @ %s | Top Ranked: %s",
                            datetime.now(timezone.utc).isoformat(),
                            top_symbols,
                        )

                        logging.info(
                            "[Layers] Target summary: %s",
                            target_summary_for_log(target),
                        )

        except asyncio.CancelledError:
            logging.info("[Layers] Layer monitor cancelled.")

            append_layer_cycle_row(
                status="cancelled",
                reason="layer_monitor_exception",
            )

            raise

        except Exception:
            logging.exception("[Layers] Layer monitor evaluation failed.")

            append_layer_cycle_row(
                status="error",
                reason="layer_monitor_exception",
            )

        finally:
            if not app_state["stream"]["shutdown_event"].is_set():
                await sleep_until_next_layer_boundary(
                    shutdown_event=app_state["stream"]["shutdown_event"],
                    interval_seconds=interval_seconds,
                    min_spacing_seconds=0.0,
                )

    logging.info("[Layers] Layer monitor exited cleanly.")
