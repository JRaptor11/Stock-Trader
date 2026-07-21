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


def _direction_family(
    direction: str | None,
) -> str:
    """
    Group target changes by strategic direction.

    INCREASE belongs to the UP family.

    DECREASE and REMOVE both belong to the DOWN family so confirmation
    can carry across a partial reduction becoming a full removal, or a
    proposed removal becoming a partial reduction.
    """
    direction = str(
        direction or ""
    ).upper().strip()

    if direction == "INCREASE":
        return "UP"

    if direction in {
        "DECREASE",
        "REMOVE",
    }:
        return "DOWN"

    return ""


def _advance_candidate(
    state: dict,
    *,
    raw_weight: float,
    direction: str,
    required: int,
    tolerance: float,
    evidence: dict,
) -> tuple[bool, str | None]:
    """
    Advance confirmation by strategic direction rather than exact weight.

    A newer target in the same directional family preserves the existing
    confirmation count, updates the pending target and subtype, and applies
    the newest subtype's required confirmation count.

    Examples:
    - INCREASE -> larger INCREASE keeps its count.
    - DECREASE -> REMOVE keeps its count.
    - REMOVE -> DECREASE keeps its count.
    - INCREASE -> DECREASE resets because the direction reversed.

    candidate_tolerance remains accepted for configuration and diagnostic
    compatibility, but it no longer resets persistent same-direction
    evidence.
    """
    old_direction = str(
        state.get(
            "pending_direction"
        )
        or ""
    ).upper()

    old_family = _direction_family(
        old_direction
    )

    new_family = _direction_family(
        direction
    )

    same_direction_family = bool(
        old_family
        and old_family == new_family
        and state.get(
            "pending_target_weight"
        ) is not None
    )

    reset_reason = None

    if not same_direction_family:
        if old_family and new_family:
            reset_reason = (
                "direction_reversed"
            )
        else:
            reset_reason = (
                "new_candidate"
            )

        state["pending_count"] = 0

    state["last_reset_reason"] = (
        reset_reason
    )

    # Always store the newest proposed destination and subtype.
    #
    # This allows:
    # - DECREASE -> REMOVE
    # - REMOVE -> DECREASE
    # - a target moving farther in the same direction
    #
    # without discarding prior same-direction confirmation.
    state.update({
        "pending_target_weight": (
            raw_weight
        ),
        "pending_direction": (
            direction
        ),
        "pending_required_count": (
            required
        ),
    })

    advanced = bool(
        evidence.get(
            "updates_allowed"
        )
    )

    if advanced:
        state["pending_count"] = (
            safe_int(
                state.get(
                    "pending_count"
                ),
                0,
            )
            + 1
        )

        state[
            "last_candidate_bar_timestamp"
        ] = evidence.get(
            "source_bar_timestamp"
        )

    return advanced, reset_reason


def _build_feasible_effective_targets(
    *,
    approved: dict[str, float],
    diagnostics: dict[str, dict],
    states: dict,
    equity: float,
    max_stock_weight: float = 1.0,
) -> dict:
    """
    Convert strategic approved targets into a feasible Layer 3 target.

    Priority order:
    1. Current held weight, up to the strategic approved target.
    2. Previously feasible target commitments that have not yet filled.
    3. Newly approved or newly available incremental target weight.

    Strategic accepted targets remain unchanged. Only the effective target
    passed to Layer 3 is capped, so portfolio feasibility does not rewrite
    hysteresis history.
    """
    limit = min(
        1.0,
        max(
            0.0,
            safe_float(
                max_stock_weight,
                1.0,
            ),
        ),
    )

    symbols = sorted(
        set(approved)
        | set(diagnostics)
        | set(states)
    )

    accepted_by_symbol: dict[str, float] = {}
    current_floor_by_symbol: dict[str, float] = {}
    previous_commitment_by_symbol: dict[str, float] = {}
    new_request_by_symbol: dict[str, float] = {}

    for symbol in symbols:
        accepted = max(
            0.0,
            safe_float(
                approved.get(symbol),
                0.0,
            ),
        )

        diagnostic = diagnostics.get(
            symbol,
            {},
        )

        current = max(
            0.0,
            safe_float(
                diagnostic.get(
                    "current_weight_at_confirmation"
                ),
                0.0,
            ),
        )

        state = states.setdefault(
            symbol,
            {},
        )

        previous_effective = max(
            0.0,
            safe_float(
                state.get(
                    "last_effective_target_weight"
                ),
                0.0,
            ),
        )

        # Protect the amount already held, but never protect more than
        # the strategically approved target.
        current_floor = min(
            accepted,
            current,
        )

        # Preserve effective allocation that was already authorized on
        # an earlier cycle but may not yet be fully represented in the
        # broker position.
        previous_commitment = max(
            0.0,
            min(
                accepted,
                previous_effective,
            )
            - current_floor,
        )

        # Everything beyond the held floor and previous commitment is a
        # newly requested increment and receives the lowest priority.
        new_request = max(
            0.0,
            accepted
            - current_floor
            - previous_commitment,
        )

        accepted_by_symbol[symbol] = (
            accepted
        )

        current_floor_by_symbol[symbol] = (
            current_floor
        )

        previous_commitment_by_symbol[symbol] = (
            previous_commitment
        )

        new_request_by_symbol[symbol] = (
            new_request
        )

    effective_by_symbol = {
        symbol: 0.0
        for symbol in symbols
    }

    def _allocate_proportionally(
        requests: dict[str, float],
        remaining: float,
    ) -> tuple[dict[str, float], float]:
        positive = {
            symbol: max(
                0.0,
                safe_float(weight, 0.0),
            )
            for symbol, weight in requests.items()
            if safe_float(weight, 0.0) > 1e-12
        }

        requested_total = sum(
            positive.values()
        )

        if (
            requested_total <= 1e-12
            or remaining <= 1e-12
        ):
            return (
                {
                    symbol: 0.0
                    for symbol in requests
                },
                max(
                    0.0,
                    remaining,
                ),
            )

        scale = min(
            1.0,
            remaining / requested_total,
        )

        allocated = {
            symbol: (
                positive.get(symbol, 0.0)
                * scale
            )
            for symbol in requests
        }

        allocated_total = sum(
            allocated.values()
        )

        return (
            allocated,
            max(
                0.0,
                remaining - allocated_total,
            ),
        )

    current_floor_total = sum(
        current_floor_by_symbol.values()
    )

    current_floor_scaled = bool(
        current_floor_total
        > limit + 1e-12
    )

    if current_floor_scaled:
        # This should be rare for the long-only paper portfolio. It is
        # retained as a final safety fallback for leverage, malformed
        # broker data, or extreme rounding conditions.
        floor_scale = (
            limit / current_floor_total
            if current_floor_total > 0
            else 0.0
        )

        for symbol in symbols:
            effective_by_symbol[symbol] = (
                current_floor_by_symbol[symbol]
                * floor_scale
            )

        remaining = 0.0

        previous_allocated = {
            symbol: 0.0
            for symbol in symbols
        }

        new_allocated = {
            symbol: 0.0
            for symbol in symbols
        }

    else:
        for symbol in symbols:
            effective_by_symbol[symbol] = (
                current_floor_by_symbol[symbol]
            )

        remaining = max(
            0.0,
            limit - current_floor_total,
        )

        (
            previous_allocated,
            remaining,
        ) = _allocate_proportionally(
            previous_commitment_by_symbol,
            remaining,
        )

        for symbol, weight in (
            previous_allocated.items()
        ):
            effective_by_symbol[symbol] += (
                weight
            )

        (
            new_allocated,
            remaining,
        ) = _allocate_proportionally(
            new_request_by_symbol,
            remaining,
        )

        for symbol, weight in (
            new_allocated.items()
        ):
            effective_by_symbol[symbol] += (
                weight
            )

    approved_total = sum(
        accepted_by_symbol.values()
    )

    deferred_by_symbol: dict[str, float] = {}

    now_iso = datetime.now(
        timezone.utc
    ).isoformat()

    for symbol in symbols:
        accepted = accepted_by_symbol[symbol]

        effective = min(
            accepted,
            max(
                0.0,
                effective_by_symbol[symbol],
            ),
        )

        deferred = max(
            0.0,
            accepted - effective,
        )

        effective_by_symbol[symbol] = (
            effective
        )

        deferred_by_symbol[symbol] = (
            deferred
        )

        state = states.setdefault(
            symbol,
            {},
        )

        state.update({
            "last_effective_target_weight": (
                effective
            ),
            "last_allocation_cap_deferred_weight": (
                deferred
            ),
            "last_effective_target_updated_at": (
                now_iso
            ),
        })

        diagnostic = diagnostics.setdefault(
            symbol,
            {},
        )

        diagnostic.update({
            "effective_target_weight": round(
                effective,
                6,
            ),
            "allocation_cap_applied": bool(
                deferred > 1e-12
            ),
            "allocation_cap_deferred_weight": round(
                deferred,
                6,
            ),
            "allocation_cap_deferred_notional": round(
                deferred * equity,
                2,
            ),
            "allocation_current_floor_weight": round(
                current_floor_by_symbol[symbol],
                6,
            ),
            "allocation_previous_commitment_weight": round(
                previous_commitment_by_symbol[symbol],
                6,
            ),
            "allocation_new_request_weight": round(
                new_request_by_symbol[symbol],
                6,
            ),
        })

    feasible_weights = {
        symbol: weight
        for symbol, weight in (
            effective_by_symbol.items()
        )
        if weight > 1e-12
    }

    deferred_total = sum(
        deferred_by_symbol.values()
    )

    return {
        "weights": feasible_weights,
        "approved_total": approved_total,
        "effective_total": sum(
            feasible_weights.values()
        ),
        "deferred_total": deferred_total,
        "cap_applied": bool(
            deferred_total > 1e-12
        ),
        "current_floor_total": (
            current_floor_total
        ),
        "previous_commitment_total": sum(
            previous_commitment_by_symbol.values()
        ),
        "new_request_total": sum(
            new_request_by_symbol.values()
        ),
        "current_floor_scaled": (
            current_floor_scaled
        ),
        "remaining_capacity": max(
            0.0,
            remaining,
        ),
    }


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

    allocation_result = (
        _build_feasible_effective_targets(
            approved=approved,
            diagnostics=diagnostics,
            states=states,
            equity=equity,
            max_stock_weight=1.0,
        )
    )

    effective_target = dict(
        allocation_result["weights"]
    )

    approved_total = safe_float(
        allocation_result.get(
            "approved_total"
        ),
        0.0,
    )

    effective_total = safe_float(
        allocation_result.get(
            "effective_total"
        ),
        0.0,
    )

    effective_target["CASH"] = max(
        0.0,
        1.0 - effective_total,
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
            "strategic_approved_stock_weight_total": (
                approved_total
            ),
            "effective_stock_weight_total": (
                effective_total
            ),
            "allocation_cap_applied": bool(
                allocation_result.get(
                    "cap_applied"
                )
            ),
            "allocation_cap_deferred_weight_total": (
                safe_float(
                    allocation_result.get(
                        "deferred_total"
                    ),
                    0.0,
                )
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
        "effective_stock_weight_total": round(
            effective_total,
            6,
        ),
        "allocation_cap_applied": bool(
            allocation_result.get(
                "cap_applied"
            )
        ),
        "allocation_cap_deferred_weight_total": round(
            safe_float(
                allocation_result.get(
                    "deferred_total"
                ),
                0.0,
            ),
            6,
        ),
        "allocation_current_floor_total": round(
            safe_float(
                allocation_result.get(
                    "current_floor_total"
                ),
                0.0,
            ),
            6,
        ),
        "allocation_previous_commitment_total": round(
            safe_float(
                allocation_result.get(
                    "previous_commitment_total"
                ),
                0.0,
            ),
            6,
        ),
        "allocation_new_request_total": round(
            safe_float(
                allocation_result.get(
                    "new_request_total"
                ),
                0.0,
            ),
            6,
        ),
        "allocation_current_floor_scaled": bool(
            allocation_result.get(
                "current_floor_scaled"
            )
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