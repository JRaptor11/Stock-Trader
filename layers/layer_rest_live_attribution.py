# layers/layer_rest_live_attribution.py

from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from layers.layer_csv import (
    LAYER_CSV_FILES,
    layer_csv_path,
    replace_layer_rest_live_attribution_cycle_rows,
    replace_layer_rest_live_attribution_symbol_rows,
)
from utils.numeric import safe_float


_ESTIMATE_BASIS = (
    "effective_shadow_weight_delta_live_minus_rest * "
    "reference_equity * forward_return"
)


def _read_all(logical_name: str) -> list[dict]:
    filename = LAYER_CSV_FILES.get(logical_name)

    if not filename:
        return []

    path = layer_csv_path(filename)

    if not path.exists():
        return []

    with path.open("r", newline="", encoding="utf-8") as f:
        return [
            dict(row)
            for row in csv.DictReader(f)
        ]


def _text(value) -> str:
    return str(value or "").strip()


def _symbol(value) -> str:
    return _text(value).upper()


def _number(value):
    if value in (None, ""):
        return None

    try:
        return float(value)
    except Exception:
        return None


def _integer(value):
    number = _number(value)

    if number is None:
        return None

    return int(number)


def _bool(value):
    if isinstance(value, bool):
        return value

    text = _text(value).lower()

    if text in {"true", "1", "yes", "on"}:
        return True

    if text in {"false", "0", "no", "off"}:
        return False

    return None


def _json(value, default=None):
    if value in (None, ""):
        return default

    if isinstance(value, (dict, list)):
        return value

    try:
        return json.loads(value)
    except Exception:
        return default


def _dt(value) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed

    except Exception:
        return None


def _nearest_row(
    rows: list[dict],
    source_timestamp: str,
) -> dict:
    """
    Find the comparison row nearest to the source-cycle timestamp.

    This is safer than joining on cycle_id alone because cycle IDs may repeat
    after a process restart.
    """
    if not rows:
        return {}

    source_dt = _dt(source_timestamp)

    if source_dt is None:
        return rows[-1]

    def distance(row: dict):
        row_dt = _dt(row.get("timestamp"))

        if row_dt is None:
            return float("inf")

        return abs(
            (row_dt - source_dt).total_seconds()
        )

    return min(rows, key=distance)


def _better_source(value) -> str | None:
    """
    Estimated P/L difference is LIVE minus REST.

    Positive:
        LIVE exposure was better.

    Negative:
        REST exposure was better.
    """
    if value is None:
        return None

    value = safe_float(value, 0.0)

    if value > 0:
        return "LIVE"

    if value < 0:
        return "REST"

    return "TIE"


def _snapshot_weight(
    strategy_snapshot: dict,
    symbol: str,
):
    """
    Return a symbol's effective portfolio weight.

    An absent symbol means the strategy holds zero weight.
    A missing or invalid strategy snapshot remains unknown
    so incomplete attribution is not silently treated as zero.
    """
    if not isinstance(
        strategy_snapshot,
        dict,
    ):
        return None

    if not strategy_snapshot:
        return None

    row = strategy_snapshot.get(
        symbol
    )

    if row is None:
        return 0.0

    if not isinstance(
        row,
        dict,
    ):
        return None

    return _number(
        row.get("weight")
    )


def _estimated_pl(
    effective_delta,
    reference_equity,
    forward_return,
):
    """
    Estimate the P/L difference caused by the strategies' effective
    post-rebalance exposure difference.

    This is a source-cycle counterfactual estimate. It is not expected to
    exactly reconcile to later total shadow equity because future cycles may
    rebalance again.
    """
    if (
        effective_delta is None
        or reference_equity is None
        or forward_return is None
    ):
        return None

    return round(
        safe_float(effective_delta, 0.0)
        * safe_float(reference_equity, 0.0)
        * safe_float(forward_return, 0.0),
        2,
    )


def _source_key(
    cycle_id,
    timestamp,
) -> tuple[str, str]:
    return (
        _text(cycle_id),
        _text(timestamp),
    )


def rebuild_rest_live_attribution_csvs() -> dict:
    """
    Rebuild joined REST-vs-LIVE attribution CSVs.

    Inputs:
    - layer_live_strategy_shadow.csv
    - layer_live_strategy_shadow_cycles.csv
    - layer_live_strategy_outcomes.csv
    - layer_strategy_shadow_comparison.csv
    - layer_strategy_shadow_portfolios.csv

    Outputs:
    - layer_rest_live_attribution_cycles.csv
    - layer_rest_live_attribution_symbols.csv

    This function is diagnostic-only:
    - it does not update Layer 1/2/3 strategy state
    - it does not modify an execution plan
    - it does not call Layer 4 or Layer 5
    - it does not submit or filter an order
    """
    try:
        symbol_source_rows = _read_all(
            "live-strategy-shadow"
        )
        cycle_source_rows = _read_all(
            "live-strategy-shadow-cycles"
        )
        outcome_rows = _read_all(
            "live-strategy-outcomes"
        )
        comparison_rows = _read_all(
            "strategy-shadow-comparison"
        )
        portfolio_rows = _read_all(
            "strategy-shadow-portfolios"
        )

        if not symbol_source_rows and not cycle_source_rows:
            return {
                "status": "no_source_rows",
                "cycle_rows": 0,
                "symbol_rows": 0,
            }

        # Exact source-cycle/symbol join for matured outcomes.
        outcomes = {}

        for row in outcome_rows:
            key = (
                _text(row.get("source_cycle_id")),
                _text(row.get("source_timestamp")),
                _symbol(row.get("symbol")),
            )
            outcomes[key] = row

        # Comparison timestamps occur slightly after the live-shadow source
        # timestamp, so group by cycle and select the nearest row.
        comparisons_by_cycle = defaultdict(list)

        for row in comparison_rows:
            comparisons_by_cycle[
                _text(row.get("cycle_id"))
            ].append(row)

        # cycle -> timestamp -> strategy -> symbol -> row
        portfolio_snapshots = defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(dict)
            )
        )

        for row in portfolio_rows:
            cycle_id = _text(row.get("cycle_id"))
            timestamp = _text(row.get("timestamp"))
            strategy = _text(
                row.get("strategy_name")
            ).upper()
            symbol = _symbol(row.get("symbol"))

            if (
                cycle_id
                and timestamp
                and strategy
                and symbol
            ):
                portfolio_snapshots[
                    cycle_id
                ][timestamp][strategy][symbol] = row

        def nearest_portfolio_snapshot(
            cycle_id: str,
            source_timestamp: str,
        ) -> dict:
            snapshots = portfolio_snapshots.get(
                cycle_id,
                {},
            )

            if not snapshots:
                return {}

            source_dt = _dt(source_timestamp)

            if source_dt is None:
                latest_timestamp = sorted(
                    snapshots
                )[-1]
                return snapshots[latest_timestamp]

            def distance(timestamp: str):
                row_dt = _dt(timestamp)

                if row_dt is None:
                    return float("inf")

                return abs(
                    (row_dt - source_dt).total_seconds()
                )

            nearest_timestamp = min(
                snapshots.keys(),
                key=distance,
            )

            return snapshots[nearest_timestamp]

        symbol_rows = []
        symbol_rows_by_cycle = defaultdict(list)
        seen_symbol_keys = set()

        for source in symbol_source_rows:
            cycle_id = _text(
                source.get("cycle_id")
            )
            timestamp = _text(
                source.get("timestamp")
            )
            symbol = _symbol(
                source.get("symbol")
            )

            if not cycle_id or not timestamp or not symbol:
                continue

            unique_key = (
                cycle_id,
                timestamp,
                symbol,
            )

            if unique_key in seen_symbol_keys:
                continue

            seen_symbol_keys.add(unique_key)

            outcome = outcomes.get(
                unique_key,
                {},
            )

            comparison = _nearest_row(
                comparisons_by_cycle.get(
                    cycle_id,
                    [],
                ),
                timestamp,
            )

            snapshot = nearest_portfolio_snapshot(
                cycle_id,
                timestamp,
            )

            rest_strategy_snapshot = snapshot.get(
                "REST",
                {},
            )

            live_strategy_snapshot = snapshot.get(
                "LIVE",
                {},
            )

            rest_equity = _number(comparison.get("rest_equity"))
            live_equity = _number(
                comparison.get("live_equity")
            )

            valid_equities = [
                value
                for value in (
                    rest_equity,
                    live_equity,
                )
                if value is not None and value > 0
            ]

            reference_equity = (
                round(
                    sum(valid_equities)
                    / len(valid_equities),
                    2,
                )
                if valid_equities
                else None
            )

            rest_strategy_snapshot = snapshot.get(
                "REST",
                {},
            )

            live_strategy_snapshot = snapshot.get(
                "LIVE",
                {},
            )

            rest_equity = _number(comparison.get("rest_equity"))

            rest_effective_weight = (
                _snapshot_weight(
                    rest_strategy_snapshot,
                    symbol,
                )
            )

            live_effective_weight = (
                _snapshot_weight(
                    live_strategy_snapshot,
                    symbol,
                )
            )

            effective_delta = (
                round(
                    live_effective_weight
                    - rest_effective_weight,
                    6,
                )
                if (
                    rest_effective_weight is not None
                    and live_effective_weight is not None
                )
                else None
            )

            forward_10m = _number(
                outcome.get("forward_return_10m")
            )
            forward_30m = _number(
                outcome.get("forward_return_30m")
            )
            forward_60m = _number(
                outcome.get("forward_return_60m")
            )

            pl_10m = _estimated_pl(
                effective_delta,
                reference_equity,
                forward_10m,
            )
            pl_30m = _estimated_pl(
                effective_delta,
                reference_equity,
                forward_30m,
            )
            pl_60m = _estimated_pl(
                effective_delta,
                reference_equity,
                forward_60m,
            )

            preferred_result = (
                pl_60m
                if pl_60m is not None
                else pl_30m
            )

            if preferred_result is None:
                preferred_result = pl_10m

            row = {
                "timestamp": timestamp,
                "outcome_timestamp": outcome.get(
                    "outcome_timestamp"
                ),
                "cycle_id": cycle_id,
                "symbol": symbol,
                "market_is_open": _bool(
                    source.get("market_is_open")
                ),
                "rest_status": source.get(
                    "rest_status"
                ),
                "live_status": source.get(
                    "live_status"
                ),
                "rest_rank": _integer(
                    source.get("rest_rank")
                ),
                "live_rank": _integer(
                    source.get("live_rank")
                ),
                "rank_delta_live_minus_rest": _integer(
                    source.get(
                        "rank_delta_live_minus_rest"
                    )
                ),
                "rest_score": _number(
                    source.get("rest_score")
                ),
                "live_score": _number(
                    source.get("live_score")
                ),
                "score_delta_live_minus_rest": _number(
                    source.get(
                        "score_delta_live_minus_rest"
                    )
                ),
                "rest_target_weight": _number(
                    source.get("rest_target_weight")
                ),
                "live_target_weight": _number(
                    source.get("live_target_weight")
                ),
                "target_weight_delta_live_minus_rest": _number(
                    source.get(
                        "target_weight_delta_live_minus_rest"
                    )
                ),
                "current_weight": _number(
                    source.get("current_weight")
                ),
                "rest_effective_shadow_weight": (
                    rest_effective_weight
                ),
                "live_effective_shadow_weight": (
                    live_effective_weight
                ),
                "effective_shadow_weight_delta_live_minus_rest": (
                    effective_delta
                ),
                "reference_equity": reference_equity,
                "rest_decision": source.get(
                    "rest_decision"
                ),
                "live_implied_decision": source.get(
                    "live_shadow_decision"
                ),
                "decision_agreement": _bool(
                    source.get("decision_agreement")
                ),
                "start_price": _number(
                    outcome.get("start_live_price")
                    or source.get("live_price")
                ),
                "outcome_price": _number(
                    outcome.get("outcome_live_price")
                ),
                "forward_return_10m": forward_10m,
                "forward_return_30m": forward_30m,
                "forward_return_60m": forward_60m,
                "estimated_pl_diff_10m": pl_10m,
                "estimated_pl_diff_30m": pl_30m,
                "estimated_pl_diff_60m": pl_60m,
                "better_source_10m": _better_source(
                    pl_10m
                ),
                "better_source_30m": _better_source(
                    pl_30m
                ),
                "better_source_60m": _better_source(
                    pl_60m
                ),
                "better_source": _better_source(
                    preferred_result
                ),
                "estimate_basis": _ESTIMATE_BASIS,
                "rest_reason": source.get(
                    "rest_reason"
                ),
                "live_reason": source.get(
                    "live_reason"
                ),
                "finalized_reason": outcome.get(
                    "finalized_reason"
                ),
                "error": comparison.get("error"),
            }

            symbol_rows.append(row)

            symbol_rows_by_cycle[
                _source_key(
                    cycle_id,
                    timestamp,
                )
            ].append(row)

        cycle_rows = []
        seen_cycle_keys = set()

        for source in cycle_source_rows:
            cycle_id = _text(
                source.get("cycle_id")
            )
            timestamp = _text(
                source.get("timestamp")
            )

            if not cycle_id or not timestamp:
                continue

            key = _source_key(
                cycle_id,
                timestamp,
            )

            if key in seen_cycle_keys:
                continue

            seen_cycle_keys.add(key)

            comparison = _nearest_row(
                comparisons_by_cycle.get(
                    cycle_id,
                    [],
                ),
                timestamp,
            )

            symbols = symbol_rows_by_cycle.get(
                key,
                [],
            )

            # Actual decision disagreement is prioritized first.
            # Target and score deltas break ties and also surface large
            # strategic differences when both decisions happen to be HOLD.
            disagreement_symbols = sorted(
                symbols,
                key=lambda row: (
                    (
                        0
                        if row.get(
                            "decision_agreement"
                        ) is False
                        else 1
                    ),
                    -abs(
                        safe_float(
                            row.get(
                                "target_weight_delta_live_minus_rest"
                            ),
                            0.0,
                        )
                    ),
                    -abs(
                        safe_float(
                            row.get(
                                "score_delta_live_minus_rest"
                            ),
                            0.0,
                        )
                    ),
                ),
            )[:5]

            top_disagreements = [
                {
                    "symbol": row.get("symbol"),
                    "rest_decision": row.get(
                        "rest_decision"
                    ),
                    "live_implied_decision": row.get(
                        "live_implied_decision"
                    ),
                    "target_weight_delta_live_minus_rest": row.get(
                        "target_weight_delta_live_minus_rest"
                    ),
                    "rank_delta_live_minus_rest": row.get(
                        "rank_delta_live_minus_rest"
                    ),
                    "score_delta_live_minus_rest": row.get(
                        "score_delta_live_minus_rest"
                    ),
                }
                for row in disagreement_symbols
            ]

            # Only use a horizon when every eligible source symbol has matured
            # to that horizon. This prevents a cycle total from mixing 10-minute
            # and 60-minute contributions.
            eligible = [
                row
                for row in symbols
                if row.get("start_price") is not None
            ]

            horizon = None
            contribution_field = None

            for minutes in (60, 30, 10):
                field = (
                    f"estimated_pl_diff_{minutes}m"
                )

                if (
                    eligible
                    and all(
                        row.get(field) is not None
                        for row in eligible
                    )
                ):
                    horizon = minutes
                    contribution_field = field
                    break

            contributions = []

            if contribution_field:
                contributions = [
                    (
                        row.get("symbol"),
                        safe_float(
                            row.get(
                                contribution_field
                            ),
                            0.0,
                        ),
                    )
                    for row in eligible
                ]

            live_advantages = [
                item
                for item in contributions
                if item[1] > 0
            ]
            rest_advantages = [
                item
                for item in contributions
                if item[1] < 0
            ]

            biggest_live = (
                max(
                    live_advantages,
                    key=lambda item: item[1],
                )
                if live_advantages
                else None
            )
            biggest_rest = (
                min(
                    rest_advantages,
                    key=lambda item: item[1],
                )
                if rest_advantages
                else None
            )

            score_diffs = [
                abs(
                    safe_float(
                        row.get(
                            "score_delta_live_minus_rest"
                        ),
                        0.0,
                    )
                )
                for row in symbols
                if row.get(
                    "score_delta_live_minus_rest"
                ) is not None
            ]

            rest_cash_target = _number(
                source.get("rest_cash_pct")
            )
            live_cash_target = _number(
                source.get("live_cash_pct")
            )

            cash_target_delta = (
                round(
                    live_cash_target
                    - rest_cash_target,
                    6,
                )
                if (
                    rest_cash_target is not None
                    and live_cash_target is not None
                )
                else None
            )

            cycle_rows.append({
                "timestamp": timestamp,
                "cycle_id": cycle_id,
                "market_is_open": _bool(
                    source.get("market_is_open")
                ),
                "rest_status": source.get(
                    "rest_status"
                ),
                "live_status": source.get(
                    "live_status"
                ),
                "rest_equity": _number(
                    comparison.get("rest_equity")
                ),
                "live_equity": _number(
                    comparison.get("live_equity")
                ),
                "live_minus_rest": _number(
                    comparison.get(
                        "live_minus_rest_equity"
                    )
                ),
                "rest_cash_target": rest_cash_target,
                "live_cash_target": live_cash_target,
                "cash_target_delta_live_minus_rest": (
                    cash_target_delta
                ),
                "rest_cash_actual": _number(
                    comparison.get("rest_cash_pct")
                ),
                "live_cash_actual": _number(
                    comparison.get("live_cash_pct")
                ),
                "target_diff_total": _number(
                    source.get(
                        "total_abs_target_weight_diff"
                    )
                ),
                "decision_agreement_rate": _number(
                    source.get(
                        "decision_agreement_rate"
                    )
                ),
                "top5_overlap_count": _integer(
                    source.get("top5_overlap_count")
                ),
                "top5_overlap_symbols": _json(
                    source.get("top5_overlap_symbols"),
                    default=[],
                ),
                "disagreement_count": sum(
                    1
                    for row in symbols
                    if row.get(
                        "decision_agreement"
                    ) is False
                ),
                "top_disagreement_symbols": (
                    top_disagreements
                ),
                "avg_abs_score_diff": (
                    round(
                        sum(score_diffs)
                        / len(score_diffs),
                        6,
                    )
                    if score_diffs
                    else None
                ),
                "attribution_horizon_minutes": (
                    horizon
                ),
                "biggest_rest_advantage_symbol": (
                    biggest_rest[0]
                    if biggest_rest
                    else None
                ),
                "biggest_rest_advantage_estimated_pl": (
                    biggest_rest[1]
                    if biggest_rest
                    else None
                ),
                "biggest_live_advantage_symbol": (
                    biggest_live[0]
                    if biggest_live
                    else None
                ),
                "biggest_live_advantage_estimated_pl": (
                    biggest_live[1]
                    if biggest_live
                    else None
                ),
                "attributed_estimated_pl_total": (
                    round(
                        sum(
                            value
                            for _, value
                            in contributions
                        ),
                        2,
                    )
                    if contribution_field
                    else None
                ),
                "symbol_count": len(symbols),
                "symbol_outcome_count": sum(
                    1
                    for row in symbols
                    if row.get(
                        "forward_return_10m"
                    ) is not None
                ),
                "error": (
                    source.get("error")
                    or comparison.get("error")
                ),
            })

        symbol_rows.sort(
            key=lambda row: (
                _text(row.get("timestamp")),
                _symbol(row.get("symbol")),
            )
        )
        cycle_rows.sort(
            key=lambda row: _text(
                row.get("timestamp")
            )
        )

        replace_layer_rest_live_attribution_symbol_rows(
            symbol_rows
        )
        replace_layer_rest_live_attribution_cycle_rows(
            cycle_rows
        )

        return {
            "status": "ok",
            "cycle_rows": len(cycle_rows),
            "symbol_rows": len(symbol_rows),
        }

    except Exception as exc:
        logging.warning(
            "[RestLiveAttribution] "
            "Failed rebuilding attribution CSVs: %s",
            exc,
            exc_info=True,
        )

        return {
            "status": "error",
            "error": str(exc),
        }