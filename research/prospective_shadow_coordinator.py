"""Restart-safe coordinator for daily prospective shadow observations."""

from __future__ import annotations

import copy
import gzip
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.prospective_shadow import (
    ShadowPortfolio, build_frozen_plan, execute_shadow_plan, execution_attribution,
    execution_reconciliation, freeze_decision, opening_quote_diagnostics,
    persist_compact_session_bundle, _hash,
)
from research.tier1_etf_replay import (
    Tier1Config, _daily_strategy_registry, _rebalance_day,
    _strategy_rebalance_frequency,
)
from research.universes import resolve_universe
from research.market_state_episodes import causal_state_labels


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
        return {"schema_version": 1, "last_session": None, "expected_next_session": None,
                "histories": {symbol: [] for symbol in symbols},
                "pending": {}, "market_state": {"active": None, "pending": None,
                                                    "pending_count": 0}, "portfolios": {
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
        if temporary.stat().st_size > 512 * 1024:
            temporary.unlink(missing_ok=True)
            raise ValueError("prospective shadow rolling state exceeds 512 KiB safety limit")
        if self.store.durable:
            self.store.upload_file(temporary, "shadow/prospective-shadow-state.json.gz")
        temporary.replace(self.state_path)

    def create_readiness_manifest(self, expected_session: str) -> dict:
        """Freeze the existing prospective state and rehearse it without mutation."""
        if self.state.get("expected_next_session") != expected_session:
            raise ValueError(
                "readiness session does not match the coordinator's expected next session"
            )
        pending = self.state.get("pending", {})
        missing = sorted(set(STRATEGIES).difference(pending))
        wrong_session = sorted(
            name for name, plan in pending.items()
            if str(plan.get("execution_session")) != expected_session
        )
        prior_closes = {
            symbol: float(values[-1]) for symbol, values in self.state["histories"].items()
            if values
        }
        missing_closes = sorted(set(resolve_universe(self.config.universe_name)) - set(prior_closes))
        rehearsal = []
        for strategy in STRATEGIES:
            plan = pending.get(strategy)
            if not plan:
                rehearsal.append({"strategy": strategy, "status": "missing_plan"})
                continue
            data = self.state["portfolios"][strategy]
            portfolio = ShadowPortfolio(
                float(data["cash"]), dict(data["positions"]), set(data["processed_plans"])
            )
            before = copy.deepcopy(portfolio)
            observations = {
                symbol: {"open": price, "volume": 10_000_000.0}
                for symbol, price in prior_closes.items()
            }
            outcome = execute_shadow_plan(plan, portfolio, observations)
            second = execute_shadow_plan(plan, portfolio, observations)
            rehearsal.append({
                "strategy": strategy,
                "status": outcome["status"],
                "fills": len(outcome.get("fills", [])),
                "rejections": len(outcome.get("rejections", [])),
                "ending_cash_nonnegative": portfolio.cash >= -1e-9,
                "idempotent_retry": second["status"] == "unchanged",
                "starting_state_unchanged": (
                    data["cash"] == before.cash and data["positions"] == before.positions
                ),
            })
        checks = {
            "expected_session_matches": True,
            "all_six_plans_present": not missing,
            "all_plans_target_expected_session": not wrong_session,
            "complete_prior_close_coverage": not missing_closes,
            "broker_orders_disabled": True,
            "all_rehearsals_executed": all(r["status"] == "executed" for r in rehearsal),
            "all_rehearsals_idempotent": all(r.get("idempotent_retry") for r in rehearsal),
            "all_rehearsal_cash_nonnegative": all(
                r.get("ending_cash_nonnegative") for r in rehearsal
            ),
        }
        payload = {
            "schema_version": 1,
            "test_name": "prospective-long-term-shadow",
            "expected_session": expected_session,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "service_code_revision": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT"),
            "strategies": list(STRATEGIES),
            "universe_name": self.config.universe_name,
            "market_state_universe_name": self.config.market_state_universe_name,
            "config": asdict(self.config),
            "config_sha256": _hash(asdict(self.config)),
            "last_completed_session": self.state.get("last_session"),
            "confirmed_market_state": (self.state.get("market_state") or {}).get("active"),
            "pending_plans": {
                name: {
                    "decision_sha256": plan.get("decision_sha256"),
                    "plan_sha256": plan.get("plan_sha256"),
                    "execution_session": plan.get("execution_session"),
                    "order_count": len(plan.get("orders", [])),
                } for name, plan in sorted(pending.items())
            },
            "portfolio_state_sha256": _hash(self.state.get("portfolios", {})),
            "checks": checks,
            "rehearsal": rehearsal,
            "missing_strategies": missing,
            "wrong_execution_session": wrong_session,
            "missing_prior_closes": missing_closes,
            "frozen_strategy_parameters_changed": False,
            "paper_orders_approved": False,
        }
        unsigned = dict(payload)
        payload["manifest_sha256"] = _hash(unsigned)
        path = self.root / f"readiness-{expected_session}.json"
        if not path.exists() and self.store.durable:
            self.store.download_file(
                f"shadow/readiness-{expected_session}.json", path
            )
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            volatile = {"created_at", "manifest_sha256"}
            comparable_existing = {k: v for k, v in existing.items() if k not in volatile}
            comparable_payload = {k: v for k, v in payload.items() if k not in volatile}
            if _hash(comparable_existing) != _hash(comparable_payload):
                raise ValueError("readiness manifest already exists with different frozen state")
            durable_uri = None
            if self.store.durable:
                durable_uri = self.store.upload_file_if_missing(
                    path, f"shadow/readiness-{expected_session}.json"
                )
            return {**existing, "status": "unchanged", "path": str(path),
                    "durable_uri": durable_uri}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        durable_uri = None
        if self.store.durable:
            durable_uri = self.store.upload_file_if_missing(
                path, f"shadow/readiness-{expected_session}.json"
            )
        return {**payload, "status": "created", "path": str(path),
                "durable_uri": durable_uri}

    def process_session(self, payload: dict) -> dict:
        session, next_session = str(payload["session"]), str(payload["next_session"])
        if self.state["last_session"] and session < self.state["last_session"]:
            raise ValueError("prospective sessions must be submitted chronologically")
        if session == self.state["last_session"]:
            return {"status": "unchanged", "session": session}
        expected_session = self.state.get("expected_next_session")
        if expected_session and session != expected_session:
            raise ValueError(
                f"unexpected prospective session: expected {expected_session}, received {session}"
            )
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
        previous_session = self.state["last_session"]
        required_symbols = set(resolve_universe(self.config.universe_name))
        missing_symbols = sorted(required_symbols.difference(bars))
        if missing_symbols:
            raise ValueError(f"incomplete prospective symbol coverage: {missing_symbols}")
        for symbol, row in bars.items():
            try:
                open_price, close = float(row["open"]), float(row["close"])
                high, low, volume = float(row["high"]), float(row["low"]), float(row["volume"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid OHLCV observation for {symbol}") from exc
            if min(open_price, close, high, low) <= 0 or volume < 0:
                raise ValueError(f"nonpositive price or negative volume for {symbol}")
            if high < max(open_price, close) or low > min(open_price, close) or high < low:
                raise ValueError(f"inconsistent OHLC range for {symbol}")
        evidence = payload.get("state_evidence")
        if not isinstance(evidence, dict):
            raise ValueError("state_evidence is required")
        derived = causal_state_labels({session: {"date": session, **evidence}}, 1).get(session)
        if not derived:
            raise ValueError("state_evidence cannot produce a canonical market state")
        raw_state = derived["raw_core_state"]
        state = self.state.setdefault("market_state", {
            "active": None, "pending": None, "pending_count": 0})
        bootstrap_state_evidence = payload.get("bootstrap_state_evidence")
        if state["active"] is None and bootstrap_state_evidence:
            if not isinstance(bootstrap_state_evidence, dict) or len(bootstrap_state_evidence) > 10:
                raise ValueError("bootstrap_state_evidence must contain at most 10 sessions")
            labels = causal_state_labels(bootstrap_state_evidence, 3)
            if labels:
                state["active"] = labels[sorted(labels)[-1]]["core_state"]
        if state["active"] is None:
            state["active"] = raw_state
        elif raw_state == state["active"]:
            state["pending"], state["pending_count"] = None, 0
        elif raw_state == state["pending"]:
            state["pending_count"] += 1
        else:
            state["pending"], state["pending_count"] = raw_state, 1
        if state["pending_count"] >= 3:
            state["active"] = state["pending"]
            state["pending"], state["pending_count"] = None, 0
        supplied_raw = payload.get("raw_state")
        supplied_confirmed = payload.get("confirmed_state")
        if supplied_raw and supplied_raw != raw_state:
            raise ValueError("supplied raw market state disagrees with causal evidence")
        if supplied_confirmed and supplied_confirmed != state["active"]:
            raise ValueError("supplied confirmed market state disagrees with coordinator state")
        warnings = []
        quote_diagnostics = opening_quote_diagnostics(
            bars, payload.get("opening_quote_diagnostics")
        )
        stale = sorted(name for name, plan in self.state["pending"].items()
                       if plan["execution_session"] < session)
        if stale:
            raise ValueError(f"stale prospective plans require review: {stale}")
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
            starting_portfolio = copy.deepcopy(portfolio)
            reconciliation = None
            if prior and prior["execution_session"] == session:
                observations = {
                    symbol: {"open": float(row["open"]), "volume": float(row.get("volume", 0))}
                    for symbol, row in bars.items()
                }
                outcome = execute_shadow_plan(prior, portfolio, observations)
                attribution = execution_attribution(
                    prior, outcome, {symbol: float(row["open"]) for symbol, row in bars.items()}
                )
                reconciliation = execution_reconciliation(
                    plan=prior, outcome=outcome,
                    starting_portfolio=starting_portfolio,
                    ending_portfolio=portfolio,
                    prior_closes={
                        symbol: float(values[-1])
                        for symbol, values in self.state["histories"].items() if values
                    },
                    bars=bars,
                )
                excessive = [row["symbol"] for row in attribution
                             if row["price_divergence_bps"] is not None
                             and abs(row["price_divergence_bps"]) > 50]
                if excessive:
                    warnings.append({"strategy": strategy,
                                     "condition": "implementation_shortfall_over_50bps",
                                     "symbols": excessive})
                self.state["pending"].pop(strategy, None)
            self.state["portfolios"][strategy] = {
                "cash": portfolio.cash, "positions": portfolio.positions,
                "processed_plans": sorted(portfolio.processed_plans),
            }
            records.append({"strategy": strategy, "executed_plan": prior,
                            "outcome": outcome, "attribution": attribution,
                            "reconciliation": reconciliation})
        for symbol, row in bars.items():
            if symbol in self.state["histories"]:
                values = self.state["histories"][symbol]
                values.append(float(row["close"])); del values[:-self.maximum_history]
        registry = _daily_strategy_registry()
        for record in records:
            strategy = record["strategy"]
            cadence = _strategy_rebalance_frequency(strategy, self.config)
            if not _rebalance_day(session, previous_session, cadence):
                record.update({"decision": None, "next_plan": None,
                               "rebalance": False, "cadence": cadence})
                continue
            snapshot = freeze_decision(
                strategy=strategy, signal_session=session, next_session=next_session,
                histories=self.state["histories"], registry=registry, config=self.config,
                raw_state=raw_state, confirmed_state=state["active"],
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
            record.update({"decision": snapshot, "next_plan": plan,
                           "rebalance": True, "cadence": cadence})
        self.state["last_session"] = session
        self.state["expected_next_session"] = next_session
        self._save()
        bundle = persist_compact_session_bundle(
            session=session, strategy_records=records, store=self.store,
            local_root=self.root / "sessions",
            session_diagnostics={
                "opening_quotes": quote_diagnostics,
                "opening_quote_collection": payload.get("opening_quote_collection") or {},
                "opening_quotes_used_for_execution": False,
                "official_daily_opens_used_for_execution": True,
            },
        )
        return {"status": "processed", "session": session,
                "next_session": next_session, "strategies": len(records),
                "raw_market_state": raw_state,
                "confirmed_market_state": state["active"],
                "warnings": warnings, "bundle": bundle}

    def status(self) -> dict:
        return {"last_session": self.state["last_session"],
                "expected_next_session": self.state.get("expected_next_session"),
                "strategies": list(STRATEGIES),
                "pending_execution_sessions": {
                    key: value["execution_session"]
                    for key, value in self.state["pending"].items()},
                "market_state": self.state.get("market_state"),
                "readiness_manifest_available": bool(
                    self.state.get("expected_next_session") and
                    (self.root / f"readiness-{self.state['expected_next_session']}.json").exists()
                ),
                "broker_orders_enabled": False}
