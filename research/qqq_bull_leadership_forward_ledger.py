"""Append-only broker-free forward ledger for frozen QQQ bull leadership."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from research.tier1_etf_replay import _canonical_raw_trend_state


UTC = timezone.utc
STRATEGY = "QQQ_BULL_TREND_LEADERSHIP"
FORWARD_START = "2026-10-27"
INITIAL_EQUITY = 100000.0
COST_BPS = 10.0
ACTIVE_STATES = {"BULL_ACCELERATING", "BULL_DECELERATING"}


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _config() -> dict:
    return {
        "strategy": STRATEGY, "forward_start": FORWARD_START,
        "active_states": sorted(ACTIVE_STATES), "active_symbol": "QQQ",
        "inactive_symbol": "SPY", "initial_equity": INITIAL_EQUITY,
        "cost_bps": COST_BPS, "no_trade_band": 0.02,
        "signal_time": "completed_close", "execution": "next_adjusted_open",
        "valuation": "adjusted_close", "broker_orders_enabled": False,
    }


def _bars(path: Path) -> dict[str, dict[str, dict[str, float]]]:
    result: dict[str, dict[str, dict[str, float]]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            symbol = row["symbol"].upper()
            if symbol not in {"SPY", "QQQ"}:
                continue
            result.setdefault(row["timestamp"][:10], {})[symbol] = {
                key: float(row[key]) for key in ("open", "close")
            }
    return result


def _target(closes: list[float]) -> tuple[str, str | None]:
    raw_state = _canonical_raw_trend_state(closes)
    return ("QQQ" if raw_state in ACTIVE_STATES else "SPY"), raw_state


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def append_session(bars_csv: Path, ledger: Path, state_path: Path,
                   session: str, next_session: str) -> dict:
    if session < FORWARD_START:
        raise ValueError("session precedes frozen forward start")
    bars = _bars(bars_csv)
    if session not in bars or set(bars[session]) != {"SPY", "QQQ"}:
        raise ValueError("completed session requires SPY and QQQ adjusted bars")
    history_dates = sorted(day for day in bars if day < session and "SPY" in bars[day])
    if len(history_dates) < 220:
        raise ValueError("insufficient strictly prior SPY history for frozen signal")
    config_hash = hashlib.sha256(_canonical(_config())).hexdigest()
    existing = _rows(ledger)
    if any(row["session"] == session for row in existing):
        return {"status": "unchanged", "session": session,
                "chain_sha256": existing[-1]["chain_sha256"]}
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None
    if state and state["config_sha256"] != config_hash:
        raise ValueError("frozen forward configuration changed")
    if state and (session <= state["last_session"] or session != state["next_session"]):
        raise ValueError("sessions must be appended in the frozen chronological order")

    prior_closes = [bars[day]["SPY"]["close"] for day in history_dates]
    opening_target, prior_raw_state = (
        (state["pending_target"], state["pending_raw_state"])
        if state else _target(prior_closes)
    )
    today = bars[session]
    rate = COST_BPS / 10000.0
    if state is None:
        investable = INITIAL_EQUITY / (1.0 + rate)
        shares = investable / today[opening_target]["open"]
        cash = INITIAL_EQUITY - investable - investable * rate
        traded = investable; modeled_cost = investable * rate
        current_symbol = opening_target; prior_equity = INITIAL_EQUITY
        spy_investable = INITIAL_EQUITY / (1.0 + rate)
        spy_shares = spy_investable / today["SPY"]["open"]
        spy_cash = INITIAL_EQUITY - spy_investable - spy_investable * rate
        prior_spy = INITIAL_EQUITY
    else:
        current_symbol = state["current_symbol"]
        shares = float(state["shares"]); cash = float(state["cash"])
        prior_equity = float(state["ending_equity"])
        spy_shares = float(state["spy_shares"]); spy_cash = float(state["spy_cash"])
        prior_spy = float(state["spy_equity"])
        traded = modeled_cost = 0.0
        if opening_target != current_symbol:
            sale = shares * today[current_symbol]["open"]
            sale_cost = sale * rate
            cash += sale - sale_cost
            investable = cash / (1.0 + rate)
            buy_cost = investable * rate
            shares = investable / today[opening_target]["open"]
            cash -= investable + buy_cost
            traded = sale + investable; modeled_cost = sale_cost + buy_cost
            current_symbol = opening_target

    ending = cash + shares * today[current_symbol]["close"]
    spy_ending = spy_cash + spy_shares * today["SPY"]["close"]
    closes_through_session = prior_closes + [today["SPY"]["close"]]
    pending_target, raw_state = _target(closes_through_session)
    high = max(float(state["high_watermark"]) if state else INITIAL_EQUITY, ending)
    spy_high = max(float(state["spy_high_watermark"]) if state else INITIAL_EQUITY, spy_ending)
    previous_chain = existing[-1]["chain_sha256"] if existing else None
    payload = {
        "session": session, "next_session": next_session,
        "recorded_at": datetime.now(UTC).isoformat(), "strategy": STRATEGY,
        "opening_signal_state": prior_raw_state, "opening_target": opening_target,
        "closing_signal_state": raw_state, "next_target": pending_target,
        "strategy_active": current_symbol == "QQQ", "current_symbol": current_symbol,
        "daily_return": ending / prior_equity - 1.0,
        "cumulative_return": ending / INITIAL_EQUITY - 1.0,
        "ending_equity": ending, "drawdown": ending / high - 1.0,
        "spy_daily_return": spy_ending / prior_spy - 1.0,
        "spy_cumulative_return": spy_ending / INITIAL_EQUITY - 1.0,
        "spy_ending_equity": spy_ending, "spy_drawdown": spy_ending / spy_high - 1.0,
        "relative_wealth_vs_spy": ending / spy_ending - 1.0,
        "gross_traded_notional": traded, "modeled_cost": modeled_cost,
        "cash": cash, "portfolio_invariants_pass": abs(cash) <= 1e-6,
        "config_sha256": config_hash, "previous_chain_sha256": previous_chain,
        "broker_orders_enabled": False, "paper_trading_approved": False,
    }
    payload["chain_sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    state_document = {
        "config_sha256": config_hash, "last_session": session,
        "next_session": next_session, "current_symbol": current_symbol,
        "shares": shares, "cash": cash, "ending_equity": ending,
        "high_watermark": high, "spy_shares": spy_shares, "spy_cash": spy_cash,
        "spy_equity": spy_ending, "spy_high_watermark": spy_high,
        "pending_target": pending_target, "pending_raw_state": raw_state,
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
