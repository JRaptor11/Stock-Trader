"""Restart-safe coordinator for daily prospective shadow observations."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from research.prospective_shadow import (
    ShadowPortfolio, build_frozen_plan, execute_shadow_plan, execution_attribution,
    freeze_decision, persist_compact_session_bundle,
)
from research.tier1_etf_replay import Tier1Config, _daily_strategy_registry
from research.universes import resolve_universe


STRATEGIES = (
    "SPY_BUY_HOLD", "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE",
    "VALUE_QUALITY_STATIC", "STATIC_INFLATION_AWARE",
    "SECTOR_ETF_ROTATION", "INDUSTRY_ETF_MOMENTUM",
)


class ProspectiveShadowCoordinator:
    def __init__(self, root: Path, store: Any, *, initial_cash: float = 100_000.0,
                 maximum_history: int = 400) -> None:
        self.root, self.store = root, store
        self.initial_cash, self.maximum_history = initial_cash, maximum_history
        self.state_path = root / "prospective-shadow-state.json.gz"
        self.config = Tier1Config(
            universe_name="ETF_LONG_TERM_RESEARCH_EXPANDED",
            market_state_universe_name="ETF_LONG_TERM_STATE_CANONICAL",
            strategy_names=STRATEGIES,
        )
        self.state = self._load()

    def _empty(self) -> dict:
        symbols = resolve_universe(self.config.universe_name)
        return {"schema_version": 1, "last_session": None,
                "histories": {symbol: [] for symbol in symbols},
                "pending": {}, "portfolios": {
                    name: {"cash": self.initial_cash, "positions": {}, "processed_plans": []}
                    for name in STRATEGIES}}

    def _load(self) -> dict:
        if not self.state_path.exists() and self.store.durable:
            self.store.download_file("shadow/prospective-shadow-state.json.gz", self.state_path)
        if not self.state_path.exists(): return self._empty()
        with gzip.open(self.state_path, "rt", encoding="utf-8") as handle:
            return json.load(handle)

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=9) as handle:
            json.dump(self.state, handle, sort_keys=True, separators=(",", ":"))
        temporary.replace(self.state_path)
        if self.state_path.stat().st_size > 512 * 1024:
            raise ValueError("prospective shadow rolling state exceeds 512 KiB safety limit")
        if self.store.durable:
            self.store.upload_file(self.state_path, "shadow/prospective-shadow-state.json.gz")

    def process_session(self, payload: dict) -> dict:
        session, next_session = str(payload["session"]), str(payload["next_session"])
        if self.state["last_session"] and session < self.state["last_session"]:
            raise ValueError("prospective sessions must be submitted chronologically")
        if session == self.state["last_session"]:
            return {"status": "unchanged", "session": session}
        bootstrap = payload.get("bootstrap_histories")
        if bootstrap is not None:
            if self.state["last_session"] is not None:
                raise ValueError("bootstrap histories are accepted only before the first session")
            if not isinstance(bootstrap, dict):
                raise ValueError("bootstrap_histories must be a symbol mapping")
            unknown = set(bootstrap).difference(self.state["histories"])
            if unknown:
                raise ValueError(f"bootstrap contains unknown symbols: {sorted(unknown)}")
            for symbol, values in bootstrap.items():
                if not isinstance(values, list) or len(values) > self.maximum_history:
                    raise ValueError("bootstrap history exceeds the rolling-history limit")
                self.state["histories"][symbol] = [float(value) for value in values]
        bars = payload["bars"]
        records = []
        for strategy in STRATEGIES:
            portfolio_data = self.state["portfolios"][strategy]
            portfolio = ShadowPortfolio(
                float(portfolio_data["cash"]),
                {key: float(value) for key, value in portfolio_data["positions"].items()},
                set(portfolio_data["processed_plans"]),
            )
            prior = self.state["pending"].get(strategy)
            outcome, attribution = None, []
            if prior and prior["execution_session"] == session:
                observations = {
                    symbol: {"open": float(row["open"]), "volume": float(row.get("volume", 0))}
                    for symbol, row in bars.items()
                }
                outcome = execute_shadow_plan(prior, portfolio, observations)
                attribution = execution_attribution(
                    prior, outcome, {symbol: float(row["open"]) for symbol, row in bars.items()}
                )
            self.state["portfolios"][strategy] = {
                "cash": portfolio.cash, "positions": portfolio.positions,
                "processed_plans": sorted(portfolio.processed_plans),
            }
            records.append({"strategy": strategy, "executed_plan": prior,
                            "outcome": outcome, "attribution": attribution})
        for symbol, row in bars.items():
            if symbol in self.state["histories"]:
                values = self.state["histories"][symbol]
                values.append(float(row["close"])); del values[:-self.maximum_history]
        registry = _daily_strategy_registry()
        for record in records:
            strategy = record["strategy"]
            snapshot = freeze_decision(
                strategy=strategy, signal_session=session, next_session=next_session,
                histories=self.state["histories"], registry=registry, config=self.config,
                raw_state=payload.get("raw_state"),
                confirmed_state=payload.get("confirmed_state"),
                data_source=str(payload.get("data_source") or "alpaca_iex_adjusted_1d"),
                source_observed_at=str(payload["source_observed_at"]),
                code_revision=str(payload["code_revision"]),
            )
            portfolio_data = self.state["portfolios"][strategy]
            portfolio = ShadowPortfolio(float(portfolio_data["cash"]),
                                        dict(portfolio_data["positions"]),
                                        set(portfolio_data["processed_plans"]))
            closes = {symbol: float(row["close"]) for symbol, row in bars.items()}
            plan = build_frozen_plan(snapshot, portfolio, closes)
            self.state["pending"][strategy] = plan
            record.update({"decision": snapshot, "next_plan": plan})
        self.state["last_session"] = session
        self._save()
        bundle = persist_compact_session_bundle(
            session=session, strategy_records=records, store=self.store,
            local_root=self.root / "sessions",
        )
        return {"status": "processed", "session": session,
                "next_session": next_session, "strategies": len(records),
                "bundle": bundle}

    def status(self) -> dict:
        return {"last_session": self.state["last_session"],
                "strategies": list(STRATEGIES),
                "pending_execution_sessions": {
                    key: value["execution_session"]
                    for key, value in self.state["pending"].items()},
                "broker_orders_enabled": False}
