"""Append-only broker-free forward ledger for frozen static 60/30/10."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


UTC = timezone.utc
STRATEGY = "STATIC_60_30_10"
TARGETS = {"SPY": 0.60, "IEF": 0.30, "GLD": 0.10}
FORWARD_START = "2026-10-12"
INITIAL_EQUITY = 100000.0
COST_BPS = 10.0


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _config() -> dict:
    return {
        "strategy": STRATEGY, "targets": TARGETS, "forward_start": FORWARD_START,
        "initial_equity": INITIAL_EQUITY, "cost_bps": COST_BPS,
        "rebalance_frequency": "monthly", "execution": "adjusted_open",
        "valuation": "adjusted_close", "broker_orders_enabled": False,
    }


def _bars(path: Path, session: str) -> dict[str, dict[str, float]]:
    selected = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row["timestamp"][:10] == session and row["symbol"] in TARGETS:
                selected[row["symbol"]] = {
                    key: float(row[key]) for key in ("open", "high", "low", "close", "volume")
                }
    missing = set(TARGETS) - set(selected)
    if missing:
        raise ValueError(f"completed session missing adjusted bars: {sorted(missing)}")
    return selected


def _load_json(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _ledger_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _initial_allocation(opening: dict[str, float], equity: float) -> tuple[dict, float, float, float]:
    rate = COST_BPS / 10000.0
    investable = equity / (1.0 + rate)
    shares = {symbol: investable * weight / opening[symbol]
              for symbol, weight in TARGETS.items()}
    traded = investable
    cost = traded * rate
    cash = equity - investable - cost
    return shares, cash, traded, cost


def _rebalance(shares: dict[str, float], cash: float, opening: dict[str, float]) -> tuple[dict, float, float, float]:
    gross = cash + sum(shares[symbol] * opening[symbol] for symbol in TARGETS)
    current = {symbol: shares[symbol] * opening[symbol] for symbol in TARGETS}
    preliminary = {symbol: gross * weight for symbol, weight in TARGETS.items()}
    one_way = sum(abs(preliminary[symbol] - current[symbol]) for symbol in TARGETS) / 2.0
    cost = one_way * COST_BPS / 10000.0
    net = gross - cost
    desired = {symbol: net * weight for symbol, weight in TARGETS.items()}
    new_shares = {symbol: desired[symbol] / opening[symbol] for symbol in TARGETS}
    new_cash = net - sum(desired.values())
    return new_shares, new_cash, one_way, cost


def append_session(bars_csv: Path, ledger: Path, state_path: Path,
                   session: str, next_session: str) -> dict:
    if session < FORWARD_START:
        raise ValueError("session precedes frozen forward start")
    config_hash = hashlib.sha256(_canonical(_config())).hexdigest()
    existing = _ledger_rows(ledger)
    if any(row["session"] == session for row in existing):
        return {"status": "unchanged", "session": session,
                "chain_sha256": existing[-1]["chain_sha256"]}
    state = _load_json(state_path)
    if state and state["config_sha256"] != config_hash:
        raise ValueError("frozen forward configuration changed")
    if state and session <= state["last_session"]:
        raise ValueError("sessions must be appended chronologically")
    if state and session != state["next_session"]:
        raise ValueError("session is not the frozen next expected session")
    bars = _bars(bars_csv, session)
    opening = {symbol: row["open"] for symbol, row in bars.items()}
    closing = {symbol: row["close"] for symbol, row in bars.items()}
    first = state is None
    if first:
        shares, cash, traded, modeled_cost = _initial_allocation(opening, INITIAL_EQUITY)
        spy_investable = INITIAL_EQUITY / (1.0 + COST_BPS / 10000.0)
        spy_shares = spy_investable / opening["SPY"]
        spy_cost = spy_investable * COST_BPS / 10000.0
        spy_cash = INITIAL_EQUITY - spy_investable - spy_cost
        prior_equity = prior_spy = INITIAL_EQUITY
        rebalanced = True
    else:
        shares = {symbol: float(state["shares"][symbol]) for symbol in TARGETS}
        cash = float(state["cash"])
        spy_shares = float(state["spy_shares"]); spy_cash = float(state["spy_cash"])
        prior_equity = float(state["ending_equity"]); prior_spy = float(state["spy_equity"])
        rebalanced = session[:7] != state["last_session"][:7]
        if rebalanced:
            shares, cash, traded, modeled_cost = _rebalance(shares, cash, opening)
        else:
            traded = modeled_cost = 0.0

    ending = cash + sum(shares[symbol] * closing[symbol] for symbol in TARGETS)
    spy_ending = spy_cash + spy_shares * closing["SPY"]
    high = max(float(state["high_watermark"]) if state else INITIAL_EQUITY, ending)
    spy_high = max(float(state["spy_high_watermark"]) if state else INITIAL_EQUITY, spy_ending)
    weights = {symbol: shares[symbol] * closing[symbol] / ending for symbol in TARGETS}
    invariant_error = abs(cash + sum(shares[s] * closing[s] for s in TARGETS) - ending)
    previous_chain = existing[-1]["chain_sha256"] if existing else None
    payload = {
        "session": session, "next_session": next_session,
        "recorded_at": datetime.now(UTC).isoformat(),
        "strategy": STRATEGY, "role": "stability_baseline",
        "daily_return": ending / prior_equity - 1.0,
        "cumulative_return": ending / INITIAL_EQUITY - 1.0,
        "ending_equity": ending, "drawdown": ending / high - 1.0,
        "spy_daily_return": spy_ending / prior_spy - 1.0,
        "spy_cumulative_return": spy_ending / INITIAL_EQUITY - 1.0,
        "spy_ending_equity": spy_ending, "spy_drawdown": spy_ending / spy_high - 1.0,
        "relative_wealth_vs_spy": ending / spy_ending - 1.0,
        "growth_retention": ((ending / INITIAL_EQUITY - 1.0) /
                             (spy_ending / INITIAL_EQUITY - 1.0)
                             if spy_ending != INITIAL_EQUITY else None),
        "rebalanced": rebalanced, "one_way_traded_notional": traded,
        "modeled_cost": modeled_cost, "cash": cash, "weights": weights,
        "portfolio_invariant_error": invariant_error,
        "portfolio_invariants_pass": invariant_error <= 1e-6 and
                                     abs(sum(weights.values()) + cash / ending - 1.0) <= 1e-6,
        "config_sha256": config_hash, "previous_chain_sha256": previous_chain,
        "broker_orders_enabled": False, "paper_trading_approved": False,
    }
    payload["chain_sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    state_document = {
        "config_sha256": config_hash, "last_session": session,
        "next_session": next_session, "shares": shares, "cash": cash,
        "ending_equity": ending, "high_watermark": high,
        "spy_shares": spy_shares, "spy_cash": spy_cash,
        "spy_equity": spy_ending, "spy_high_watermark": spy_high,
        "chain_sha256": payload["chain_sha256"], "broker_orders_enabled": False,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state_document, indent=2, sort_keys=True), encoding="utf-8")
    return {"status": "appended", **payload}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--next-session", required=True)
    args = parser.parse_args()
    print(json.dumps(append_session(args.bars, args.ledger, args.state,
                                    args.session, args.next_session), indent=2))


if __name__ == "__main__":
    main()
