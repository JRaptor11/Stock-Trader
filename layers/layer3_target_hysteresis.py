# layers/layer3_target_hysteresis.py

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from utils.numeric import safe_float, safe_int


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        return (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc)
        )

    except Exception:
        return None


def normalize_source_bar_timestamp(
    value: Any,
) -> str | None:
    parsed = _utc(value)
    return parsed.isoformat() if parsed else None


def prepare_confirmation_evidence(
    *,
    planner_state: dict,
    source_bar_timestamp: Any,
    market_is_open: bool,
    market_hours_only: bool,
) -> dict:
    """
    Allow confirmation updates only once for each newer source bar.

    REST production, LIVE common shadow, REST simulator, and LIVE simulator
    each pass their own planner_state, so their evidence remains isolated.
    """
    current = normalize_source_bar_timestamp(
        source_bar_timestamp
    )

    previous = normalize_source_bar_timestamp(
        planner_state.get(
            "last_confirmation_bar_timestamp"
        )
    )

    market_allowed = bool(
        market_is_open
        or not market_hours_only
    )

    is_new = False
    blocked_reason = None

    if not market_allowed:
        blocked_reason = "market_closed"

    elif current is None:
        blocked_reason = (
            "missing_source_bar_timestamp"
        )

    elif previous is None:
        is_new = True

    else:
        current_dt = _utc(current)
        previous_dt = _utc(previous)

        if (
            current_dt is None
            or previous_dt is None
        ):
            blocked_reason = (
                "invalid_source_bar_timestamp"
            )

        elif current_dt > previous_dt:
            is_new = True

        elif current_dt == previous_dt:
            blocked_reason = (
                "duplicate_source_bar"
            )

        else:
            blocked_reason = "older_source_bar"

    updates_allowed = bool(
        market_allowed
        and is_new
    )

    if updates_allowed:
        planner_state[
            "last_confirmation_bar_timestamp"
        ] = current

        planner_state[
            "last_confirmation_update_at"
        ] = datetime.now(
            timezone.utc
        ).isoformat()

    planner_state.update({
        "market_is_open": bool(
            market_is_open
        ),
        "confirmation_updates_allowed": (
            updates_allowed
        ),
        "confirmation_updates_blocked_reason": (
            blocked_reason
        ),
        "confirmation_source_bar_timestamp": (
            current
        ),
        "confirmation_source_bar_is_new": (
            is_new
        ),
    })

    return {
        "source_bar_timestamp": current,
        "previous_source_bar_timestamp": (
            previous
        ),
        "bar_is_new": is_new,
        "updates_allowed": updates_allowed,
        "blocked_reason": blocked_reason,
    }


def _position_weight(
    position: dict | None,
    equity: float,
) -> float:
    if (
        not isinstance(position, dict)
        or equity <= 0
    ):
        return 0.0

    value = safe_float(
        position.get("market_value"),
        0.0,
    )

    if value <= 0:
        qty = safe_float(
            position.get("qty"),
            0.0,
        )

        price = safe_float(
            position.get("current_price"),
            safe_float(
                position.get(
                    "avg_entry_price"
                ),
                0.0,
            ),
        )

        value = qty * price

    return max(
        0.0,
        value / equity,
    )


def _clear_pending(
    state: dict,
    reason: str | None,
) -> None:
    state.update({
        "pending_target_weight": None,
        "pending_direction": None,
        "pending_count": 0,
        "pending_required_count": 0,
        "last_reset_reason": reason,
    })


def _advance_candidate(
    state: dict,
    *,
    raw_weight: float,
    direction: str,
    required: int,
    tolerance: float,
    evidence: dict,
) -> tuple[bool, str | None]:
    old_direction = str(
        state.get("pending_direction")
        or ""
    ).upper()

    old_weight = state.get(
        "pending_target_weight"
    )

    same_candidate = bool(
        old_direction == direction
        and old_weight is not None
        and abs(
            safe_float(old_weight, 0.0)
            - raw_weight
        )
        <= tolerance
    )

    reset_reason = None

    if not same_candidate:
        if (
            old_direction
            and old_direction != direction
        ):
            reset_reason = "direction_reversed"

        elif old_weight is not None:
            reset_reason = (
                "candidate_outside_tolerance"
            )

        else:
            reset_reason = "new_candidate"

        state.update({
            "pending_target_weight": (
                raw_weight
            ),
            "pending_direction": direction,
            "pending_count": 0,
            "pending_required_count": (
                required
            ),
        })

    advanced = bool(
        evidence.get("updates_allowed")
    )

    if advanced:
        state["pending_count"] = (
            safe_int(
                state.get("pending_count"),
                0,
            )
            + 1
        )

        state[
            "pending_target_weight"
        ] = raw_weight

        state[
            "last_candidate_bar_timestamp"
        ] = evidence.get(
            "source_bar_timestamp"
        )

    return advanced, reset_reason


def resolve_target_hysteresis(
    *,
    planner_state: dict,
    raw_target: dict,
    raw_target_weights: dict,
    positions: dict,
    equity: float,
    evidence: dict,
    enabled: bool,
    material_change: float,
    candidate_tolerance: float,
    increase_required_count: int,
    decrease_required_count: int,
    removal_required_count: int,
    bootstrap_accepted_symbols: (
        set[str] | None
    ) = None,
) -> dict:
    """
    Approve Layer 2 targets using asymmetric, distinct-bar hysteresis.

    Behavior:
    - New/increased targets: two distinct bars by default.
    - Partial reductions: one distinct bar by default.
    - Complete removal: two distinct bars by default.
    - Duplicate or older bars never advance confirmation.
    """
    material = max(
        0.0,
        float(material_change),
    )

    tolerance = max(
        0.0,
        float(candidate_tolerance),
    )

    increase_required = max(
        1,
        int(increase_required_count),
    )

    decrease_required = max(
        1,
        int(decrease_required_count),
    )

    removal_required = max(
        1,
        int(removal_required_count),
    )

    bootstrap = {
        str(symbol or "").upper().strip()
        for symbol in (
            bootstrap_accepted_symbols
            or set()
        )
        if str(
            symbol or ""
        ).upper().strip()
    }

    states = planner_state.setdefault(
        "target_hysteresis_by_symbol",
        {},
    )

    symbols = sorted(
        set(raw_target_weights)
        | set(positions)
        | set(states)
    )

    approved: dict[str, float] = {}
    diagnostics: dict[str, dict] = {}
    actions: Counter = Counter()
    pending_symbols: list[str] = []
    changed_symbols: list[str] = []

    now_iso = datetime.now(
        timezone.utc
    ).isoformat()

    for symbol in symbols:
        raw = max(
            0.0,
            safe_float(
                raw_target_weights.get(symbol),
                0.0,
            ),
        )

        current = _position_weight(
            positions.get(symbol),
            equity,
        )

        state = states.setdefault(
            symbol,
            {},
        )

        initialized = bool(
            state.get("initialized")
        )

        previous = max(
            0.0,
            safe_float(
                state.get(
                    "accepted_target_weight"
                ),
                0.0,
            ),
        )

        accepted = previous
        action = "unchanged"
        reset_reason = None
        advanced = False
        changed = False

        if not enabled:
            accepted = raw

            changed = (
                abs(accepted - previous)
                > 1e-12
            )

            action = (
                "disabled_raw_target_used"
            )

            _clear_pending(
                state,
                "hysteresis_disabled",
            )

        elif (
            not initialized
            and symbol in bootstrap
        ):
            accepted = raw

            changed = (
                abs(accepted - previous)
                > 1e-12
            )

            action = (
                "bootstrap_target_accepted"
            )

            state["bootstrap_accepted"] = (
                True
            )

            _clear_pending(
                state,
                "bootstrap_accepted",
            )

        elif not initialized:
            if (
                raw <= 0
                and current > 0
            ):
                accepted = current

                (
                    advanced,
                    reset_reason,
                ) = _advance_candidate(
                    state,
                    raw_weight=0.0,
                    direction="REMOVE",
                    required=(
                        removal_required
                    ),
                    tolerance=tolerance,
                    evidence=evidence,
                )

                action = (
                    "initial_removal_pending"
                )

            elif (
                raw
                >= current
                + material
                - 1e-12
            ):
                accepted = current

                (
                    advanced,
                    reset_reason,
                ) = _advance_candidate(
                    state,
                    raw_weight=raw,
                    direction="INCREASE",
                    required=(
                        increase_required
                    ),
                    tolerance=tolerance,
                    evidence=evidence,
                )

                action = (
                    "initial_increase_pending"
                )

            elif (
                raw
                <= max(
                    0.0,
                    current
                    - material
                    + 1e-12,
                )
            ):
                if evidence.get(
                    "updates_allowed"
                ):
                    accepted = raw

                    changed = (
                        abs(
                            accepted
                            - previous
                        )
                        > 1e-12
                    )

                    action = (
                        "initial_decrease_"
                        "accepted"
                    )

                else:
                    accepted = current

                    action = (
                        "initial_decrease_"
                        "waiting_for_new_bar"
                    )

                _clear_pending(
                    state,
                    "initial_decrease",
                )

            else:
                accepted = raw

                changed = (
                    abs(
                        accepted
                        - previous
                    )
                    > 1e-12
                )

                action = (
                    "initial_target_accepted"
                )

                _clear_pending(
                    state,
                    "initial_within_threshold",
                )

        elif not evidence.get(
            "updates_allowed"
        ):
            accepted = previous

            pending_direction = str(
                state.get(
                    "pending_direction"
                )
                or ""
            ).lower()

            action = (
                f"{pending_direction}_"
                "pending_frozen"
                if pending_direction
                else "confirmation_frozen"
            )

        else:
            difference = raw - previous

            if (
                raw <= 0
                and previous > 0
            ):
                (
                    advanced,
                    reset_reason,
                ) = _advance_candidate(
                    state,
                    raw_weight=0.0,
                    direction="REMOVE",
                    required=(
                        removal_required
                    ),
                    tolerance=tolerance,
                    evidence=evidence,
                )

                if (
                    safe_int(
                        state.get(
                            "pending_count"
                        ),
                        0,
                    )
                    >= removal_required
                ):
                    accepted = 0.0
                    changed = True
                    action = (
                        "removal_accepted"
                    )

                    _clear_pending(
                        state,
                        "candidate_confirmed",
                    )

                else:
                    action = (
                        "removal_pending"
                    )

            elif (
                difference
                >= material
                - 1e-12
            ):
                (
                    advanced,
                    reset_reason,
                ) = _advance_candidate(
                    state,
                    raw_weight=raw,
                    direction="INCREASE",
                    required=(
                        increase_required
                    ),
                    tolerance=tolerance,
                    evidence=evidence,
                )

                if (
                    safe_int(
                        state.get(
                            "pending_count"
                        ),
                        0,
                    )
                    >= increase_required
                ):
                    accepted = raw
                    changed = True
                    action = (
                        "increase_accepted"
                    )

                    _clear_pending(
                        state,
                        "candidate_confirmed",
                    )

                else:
                    action = (
                        "increase_pending"
                    )

            elif (
                difference
                <= -material
                + 1e-12
            ):
                (
                    advanced,
                    reset_reason,
                ) = _advance_candidate(
                    state,
                    raw_weight=raw,
                    direction="DECREASE",
                    required=(
                        decrease_required
                    ),
                    tolerance=tolerance,
                    evidence=evidence,
                )

                if (
                    safe_int(
                        state.get(
                            "pending_count"
                        ),
                        0,
                    )
                    >= decrease_required
                ):
                    accepted = raw
                    changed = True
                    action = (
                        "decrease_accepted"
                    )

                    _clear_pending(
                        state,
                        "candidate_confirmed",
                    )

                else:
                    action = (
                        "decrease_pending"
                    )

            else:
                had_pending = bool(
                    state.get(
                        "pending_direction"
                    )
                    or state.get(
                        "pending_target_weight"
                    )
                    is not None
                )

                _clear_pending(
                    state,
                    (
                        "returned_within_"
                        "material_threshold"
                        if had_pending
                        else (
                            "within_material_"
                            "threshold"
                        )
                    ),
                )

                action = (
                    "within_material_threshold"
                )

        state.update({
            "initialized": True,
            "accepted_target_weight": max(
                0.0,
                accepted,
            ),
            "last_raw_target_weight": raw,
            "last_current_weight": current,
            "updated_at": now_iso,
        })

        accepted = state[
            "accepted_target_weight"
        ]

        pending_weight = state.get(
            "pending_target_weight"
        )

        pending_direction = state.get(
            "pending_direction"
        )

        pending_count = safe_int(
            state.get("pending_count"),
            0,
        )

        required_count = safe_int(
            state.get(
                "pending_required_count"
            ),
            0,
        )

        if accepted > 0:
            approved[symbol] = accepted

        if pending_direction:
            pending_symbols.append(symbol)

        if changed:
            changed_symbols.append(symbol)

        deferred_weight = (
            raw - accepted
        )

        diagnostics[symbol] = {
            "raw_target_weight": round(
                raw,
                6,
            ),
            "approved_target_weight": round(
                accepted,
                6,
            ),
            "previous_approved_target_weight": round(
                previous,
                6,
            ),
            "pending_target_weight": (
                round(
                    safe_float(
                        pending_weight,
                        0.0,
                    ),
                    6,
                )
                if pending_weight is not None
                else None
            ),
            "target_candidate_direction": (
                pending_direction
            ),
            "target_candidate_count": (
                pending_count
            ),
            "target_required_count": (
                required_count
            ),
            "target_confirmation_advanced": (
                advanced
            ),
            "target_confirmation_bar_timestamp": (
                evidence.get(
                    "source_bar_timestamp"
                )
            ),
            "target_confirmation_bar_is_new": bool(
                evidence.get(
                    "bar_is_new"
                )
            ),
            "target_hysteresis_action": (
                action
            ),
            "target_hysteresis_reset_reason": (
                reset_reason
                or state.get(
                    "last_reset_reason"
                )
            ),
            "target_hysteresis_changed_target": (
                changed
            ),
            "deferred_target_weight": round(
                deferred_weight,
                6,
            ),
            "deferred_notional": round(
                deferred_weight * equity,
                2,
            ),
            "current_weight_at_confirmation": round(
                current,
                6,
            ),
        }

        actions[action] += 1

    effective_target = dict(approved)

    approved_total = sum(
        approved.values()
    )

    effective_target["CASH"] = max(
        0.0,
        1.0 - approved_total,
    )

    raw_meta = (
        raw_target.get("_meta", {})
        if isinstance(raw_target, dict)
        else {}
    )

    if isinstance(raw_meta, dict):
        effective_target["_meta"] = {
            **raw_meta,
            "target_hysteresis_applied": bool(
                enabled
            ),
            "raw_cash_pct": safe_float(
                raw_target.get("CASH"),
                0.0,
            ),
            "approved_cash_pct": (
                effective_target["CASH"]
            ),
        }

    summary = {
        "enabled": bool(enabled),
        "source_bar_timestamp": (
            evidence.get(
                "source_bar_timestamp"
            )
        ),
        "source_bar_is_new": bool(
            evidence.get("bar_is_new")
        ),
        "confirmation_updates_allowed": bool(
            evidence.get(
                "updates_allowed"
            )
        ),
        "confirmation_blocked_reason": (
            evidence.get(
                "blocked_reason"
            )
        ),
        "material_change": material,
        "candidate_tolerance": tolerance,
        "increase_required_count": (
            increase_required
        ),
        "decrease_required_count": (
            decrease_required
        ),
        "removal_required_count": (
            removal_required
        ),
        "action_counts": dict(actions),
        "pending_symbols": sorted(
            pending_symbols
        ),
        "changed_symbols": sorted(
            changed_symbols
        ),
        "raw_target_symbol_count": len(
            raw_target_weights
        ),
        "approved_target_symbol_count": (
            len(approved)
        ),
        "raw_stock_weight_total": round(
            sum(
                raw_target_weights.values()
            ),
            6,
        ),
        "approved_stock_weight_total": round(
            approved_total,
            6,
        ),
        "approved_cash_pct": round(
            effective_target["CASH"],
            6,
        ),
    }

    planner_state[
        "last_target_hysteresis_summary"
    ] = summary

    planner_state[
        "last_target_hysteresis_rows"
    ] = diagnostics

    return {
        "effective_target": (
            effective_target
        ),
        "approved_target_weights": (
            approved
        ),
        "diagnostics_by_symbol": (
            diagnostics
        ),
        "summary": summary,
    }