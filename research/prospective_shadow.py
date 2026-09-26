"""Prospective, broker-realistic shadow execution for frozen daily strategies.

This module never submits an order.  It freezes the output of the same daily
strategy registry used by historical replay and later evaluates that plan
against prices which became observable at the next session open.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from research.daily_strategy_interface import DailyStrategyRegistry


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _config_snapshot(config: Any) -> dict:
    if is_dataclass(config):
        return asdict(config)
    if isinstance(config, Mapping):
        return dict(config)
    return {
        key: value for key, value in vars(config).items()
        if not key.startswith("_") and not callable(value)
    }


def freeze_decision(
    *, strategy: str, signal_session: str, next_session: str,
    histories: Mapping[str, list[float]], registry: DailyStrategyRegistry,
    config: Any, raw_state: str | None = None,
    confirmed_state: str | None = None, data_source: str,
    source_observed_at: str, code_revision: str,
) -> dict:
    """Freeze a causal close-time decision produced by the shared registry."""
    targets = registry.targets(strategy, histories, config)
    inputs = {
        symbol: {
            "observations": len(values),
            "last_close": float(values[-1]) if values else None,
            "history_sha256": _hash([float(value) for value in values]),
        }
        for symbol, values in sorted(histories.items())
    }
    payload = {
        "schema_version": 1,
        "strategy": strategy,
        "signal_session": signal_session,
        "execution_session": next_session,
        "signal_timing": registry.spec(strategy).signal_timing,
        "execution_timing": registry.spec(strategy).execution_timing,
        "data_source": data_source,
        "source_observed_at": source_observed_at,
        "code_revision": code_revision,
        "config": _config_snapshot(config),
        "config_sha256": _hash(_config_snapshot(config)),
        "inputs": inputs,
        "raw_market_state": raw_state,
        "confirmed_market_state": confirmed_state,
        "targets": dict(sorted(targets.items())),
        "paper_trading_approved": False,
    }
    payload["decision_sha256"] = _hash(payload)
    return payload


def write_immutable_snapshot(snapshot: dict, path: Path) -> dict:
    """Create an immutable snapshot, accepting only an identical retry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(snapshot, sort_keys=True, indent=2) + "\n"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if _canonical(existing) != _canonical(snapshot):
            raise ValueError("prospective decision snapshot already exists with different content")
        return {"status": "unchanged", "path": str(path),
                "decision_sha256": snapshot["decision_sha256"]}
    path.write_text(content, encoding="utf-8")
    return {"status": "created", "path": str(path),
            "decision_sha256": snapshot["decision_sha256"]}


@dataclass(frozen=True)
class ShadowExecutionAssumptions:
    spread_bps: float = 10.0
    slippage_bps: float = 5.0
    maximum_bar_participation: float = 0.01
    minimum_notional: float = 1.0
    fractional_shares: bool = True


@dataclass
class ShadowPortfolio:
    cash: float
    positions: dict[str, float] = field(default_factory=dict)
    processed_plans: set[str] = field(default_factory=set)


def save_portfolio(portfolio: ShadowPortfolio, path: Path) -> dict:
    """Atomically checkpoint a virtual portfolio for restart recovery."""
    payload = {
        "schema_version": 1,
        "cash": portfolio.cash,
        "positions": dict(sorted(portfolio.positions.items())),
        "processed_plans": sorted(portfolio.processed_plans),
    }
    payload["state_sha256"] = _hash(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
    return payload


def load_portfolio(path: Path, initial_cash: float | None = None) -> ShadowPortfolio:
    if not path.exists():
        if initial_cash is None:
            raise FileNotFoundError(path)
        return ShadowPortfolio(cash=float(initial_cash))
    payload = json.loads(path.read_text(encoding="utf-8"))
    stored = payload.pop("state_sha256", None)
    if stored != _hash(payload):
        raise ValueError("prospective shadow portfolio checkpoint is corrupt")
    return ShadowPortfolio(
        cash=float(payload["cash"]),
        positions={key: float(value) for key, value in payload["positions"].items()},
        processed_plans=set(payload["processed_plans"]),
    )


def append_execution_journal(record: dict, path: Path) -> dict:
    """Append a hash-chained execution result, rejecting duplicate mutation."""
    existing = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if path.exists() else []
    previous = None
    seen = {}
    for row in existing:
        stored = row.get("chain_sha256")
        unsigned = {key: value for key, value in row.items() if key != "chain_sha256"}
        if unsigned.get("previous_chain_sha256") != previous or stored != _hash(unsigned):
            raise ValueError("prospective execution journal chain is invalid")
        previous = stored
        seen[row["plan_sha256"]] = row
    if record["plan_sha256"] in seen:
        if seen[record["plan_sha256"]]["outcome_sha256"] != _hash(record):
            raise ValueError("prospective plan already journaled with a different outcome")
        return {"status": "unchanged", "chain_sha256": previous}
    payload = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "plan_sha256": record["plan_sha256"],
        "outcome_sha256": _hash(record),
        "outcome": record,
        "previous_chain_sha256": previous,
    }
    payload["chain_sha256"] = _hash(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return {"status": "appended", "chain_sha256": payload["chain_sha256"]}


def build_frozen_plan(snapshot: dict, portfolio: ShadowPortfolio,
                      reference_prices: Mapping[str, float]) -> dict:
    """Convert frozen weights to signed notionals without using future prices."""
    equity = portfolio.cash + sum(
        quantity * float(reference_prices.get(symbol, 0.0))
        for symbol, quantity in portfolio.positions.items()
    )
    symbols = set(portfolio.positions) | set(snapshot["targets"])
    no_trade_band = float(snapshot.get("config", {}).get("no_trade_band", 0.0))
    orders = []
    for symbol in sorted(symbols):
        price = float(reference_prices.get(symbol, 0.0))
        if price <= 0:
            continue
        current = portfolio.positions.get(symbol, 0.0) * price
        desired = equity * float(snapshot["targets"].get(symbol, 0.0))
        delta = desired - current
        if equity > 0 and abs(delta) / equity < no_trade_band:
            continue
        if abs(delta) >= 1e-9:
            orders.append({"symbol": symbol, "notional": delta,
                           "side": "buy" if delta > 0 else "sell"})
    unsigned = {
        "schema_version": 1,
        "decision_sha256": snapshot["decision_sha256"],
        "signal_session": snapshot["signal_session"],
        "execution_session": snapshot["execution_session"],
        "reference_equity": equity,
        "orders": orders,
        "paper_trading_approved": False,
    }
    return {**unsigned, "plan_sha256": _hash(unsigned)}


def execute_shadow_plan(
    plan: dict, portfolio: ShadowPortfolio,
    observations: Mapping[str, Mapping[str, float]],
    assumptions: ShadowExecutionAssumptions = ShadowExecutionAssumptions(),
) -> dict:
    """Execute sells before buys with capacity, cash, rounding and idempotency."""
    plan_id = plan["plan_sha256"]
    if plan_id in portfolio.processed_plans:
        return {"status": "unchanged", "plan_sha256": plan_id,
                "fills": [], "rejections": [], "ending_cash": portfolio.cash}
    fills, rejections = [], []
    orders = sorted(plan["orders"], key=lambda row: (row["notional"] > 0, row["symbol"]))
    for order in orders:
        symbol, requested = order["symbol"], float(order["notional"])
        market = observations.get(symbol)
        if not market or float(market.get("open", 0.0)) <= 0:
            rejections.append({**order, "reason": "missing_open_observation"})
            continue
        open_price = float(market["open"])
        half_spread = assumptions.spread_bps / 20000.0
        slippage = assumptions.slippage_bps / 10000.0
        fill_price = open_price * (1 + half_spread + slippage if requested > 0
                                   else 1 - half_spread - slippage)
        capacity = open_price * float(market.get("volume", 0.0)) * assumptions.maximum_bar_participation
        executable = math.copysign(min(abs(requested), capacity), requested) if capacity > 0 else 0.0
        if executable < 0:
            held = portfolio.positions.get(symbol, 0.0) * fill_price
            executable = -min(abs(executable), held)
        else:
            executable = min(executable, max(0.0, portfolio.cash))
        if abs(executable) < assumptions.minimum_notional:
            rejections.append({**order, "reason": "below_minimum_or_no_capacity",
                               "available_capacity": capacity})
            continue
        quantity = abs(executable) / fill_price
        if not assumptions.fractional_shares:
            quantity = math.floor(quantity)
            executable = math.copysign(quantity * fill_price, executable)
        if quantity <= 0:
            rejections.append({**order, "reason": "rounding_removed_order"})
            continue
        signed_quantity = quantity if executable > 0 else -quantity
        portfolio.cash -= executable
        portfolio.positions[symbol] = portfolio.positions.get(symbol, 0.0) + signed_quantity
        if abs(portfolio.positions[symbol]) < 1e-10:
            portfolio.positions.pop(symbol, None)
        fills.append({**order, "requested_notional": requested,
                      "filled_notional": executable, "quantity": signed_quantity,
                      "open": open_price, "fill_price": fill_price,
                      "partial": abs(executable) + 1e-9 < abs(requested)})
    portfolio.processed_plans.add(plan_id)
    return {
        "status": "executed",
        "plan_sha256": plan_id,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "fills": fills,
        "rejections": rejections,
        "ending_cash": portfolio.cash,
        "ending_positions": dict(sorted(portfolio.positions.items())),
        "paper_orders_submitted": 0,
    }


def execution_attribution(plan: dict, outcome: dict,
                          research_open_prices: Mapping[str, float]) -> list[dict]:
    """Attribute observed shadow divergence from ideal open-price notionals."""
    fills = {row["symbol"]: row for row in outcome.get("fills", [])}
    rejections = {row["symbol"]: row for row in outcome.get("rejections", [])}
    rows = []
    for order in plan["orders"]:
        symbol, requested = order["symbol"], float(order["notional"])
        fill = fills.get(symbol)
        reason = rejections.get(symbol, {}).get("reason")
        ideal_price = float(research_open_prices.get(symbol, 0.0))
        rows.append({
            "symbol": symbol,
            "side": order["side"],
            "requested_notional": requested,
            "filled_notional": fill["filled_notional"] if fill else 0.0,
            "research_open": ideal_price or None,
            "observed_fill": fill["fill_price"] if fill else None,
            "price_divergence_bps": (
                (fill["fill_price"] / ideal_price - 1.0) * 10000
                if fill and ideal_price else None
            ),
            "unfilled_notional": requested - (fill["filled_notional"] if fill else 0.0),
            "divergence_reason": reason or ("partial_fill" if fill and fill["partial"] else "filled"),
        })
    return rows
