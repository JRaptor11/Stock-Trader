import json
import unittest
import uuid
from pathlib import Path

from research.prospective_shadow import (
    ShadowExecutionAssumptions,
    ShadowPortfolio,
    append_execution_journal,
    build_frozen_plan,
    execute_shadow_plan,
    execution_reconciliation,
    execution_attribution,
    freeze_decision,
    load_portfolio,
    opening_quote_diagnostics,
    persist_compact_session_bundle,
    save_portfolio,
    write_immutable_snapshot,
)
from research.tier1_etf_replay import Tier1Config, _daily_strategy_registry, _targets
from research.universes import resolve_universe


class ProspectiveShadowTests(unittest.TestCase):
    def setUp(self):
        self.config = Tier1Config(universe_name="ETF_LONG_TERM_RESEARCH_EXPANDED")
        self.histories = {
            symbol: [100.0 + index * 0.1 for index in range(260)]
            for symbol in resolve_universe(self.config.universe_name)
        }

    def snapshot(self, strategy="VALUE_QUALITY_STATIC"):
        return freeze_decision(
            strategy=strategy,
            signal_session="2026-09-25",
            next_session="2026-09-28",
            histories=self.histories,
            registry=_daily_strategy_registry(),
            config=self.config,
            raw_state="BULL_ACCELERATING__NORMAL_VOL__BROAD_BREADTH",
            confirmed_state="BULL_ACCELERATING__NORMAL_VOL__BROAD_BREADTH",
            data_source="alpaca_iex_adjusted_1d",
            source_observed_at="2026-09-25T21:05:00Z",
            code_revision="test-revision",
        )

    def test_leading_strategies_use_exact_replay_targets(self):
        for strategy in (
            "SPY_BUY_HOLD", "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE",
            "VALUE_QUALITY_STATIC", "STATIC_INFLATION_AWARE",
            "SECTOR_ETF_ROTATION", "INDUSTRY_ETF_MOMENTUM",
        ):
            snapshot = self.snapshot(strategy)
            self.assertEqual(_targets(strategy, self.histories, self.config),
                             snapshot["targets"], strategy)
            self.assertFalse(snapshot["paper_trading_approved"])

    def test_snapshot_is_immutable_and_retry_is_idempotent(self):
        snapshot = self.snapshot()
        folder = Path(".test-prospective-shadow") / uuid.uuid4().hex
        folder.mkdir(parents=True)
        try:
            path = folder / "decision.json"
            self.assertEqual("created", write_immutable_snapshot(snapshot, path)["status"])
            self.assertEqual("unchanged", write_immutable_snapshot(snapshot, path)["status"])
            changed = json.loads(json.dumps(snapshot)); changed["targets"]["VLUE"] = .4
            with self.assertRaisesRegex(ValueError, "different content"):
                write_immutable_snapshot(changed, path)
        finally:
            path.unlink(missing_ok=True)
            folder.rmdir()

    def test_plan_sells_before_buys_and_is_idempotent(self):
        snapshot = self.snapshot()
        portfolio = ShadowPortfolio(cash=0.0, positions={"SPY": 100.0})
        prices = {symbol: 100.0 for symbol in self.histories}
        plan = build_frozen_plan(snapshot, portfolio, prices)
        observations = {
            symbol: {"open": 100.0, "volume": 1_000_000.0}
            for symbol in {row["symbol"] for row in plan["orders"]}
        }
        result = execute_shadow_plan(plan, portfolio, observations)
        self.assertEqual("executed", result["status"])
        self.assertNotIn("SPY", portfolio.positions)
        self.assertIn("QUAL", portfolio.positions)
        self.assertIn("VLUE", portfolio.positions)
        cash_after = portfolio.cash
        self.assertEqual("unchanged", execute_shadow_plan(plan, portfolio, observations)["status"])
        self.assertEqual(cash_after, portfolio.cash)

    def test_capacity_and_missing_data_are_attributed(self):
        snapshot = self.snapshot()
        portfolio = ShadowPortfolio(cash=10_000.0)
        prices = {symbol: 100.0 for symbol in self.histories}
        plan = build_frozen_plan(snapshot, portfolio, prices)
        observations = {"QUAL": {"open": 101.0, "volume": 10.0}}
        result = execute_shadow_plan(
            plan, portfolio, observations,
            ShadowExecutionAssumptions(maximum_bar_participation=.01),
        )
        rows = execution_attribution(plan, result, {"QUAL": 100.0, "VLUE": 100.0})
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual("partial_fill", by_symbol["QUAL"]["divergence_reason"])
        self.assertEqual("missing_open_observation", by_symbol["VLUE"]["divergence_reason"])
        self.assertGreater(by_symbol["QUAL"]["price_divergence_bps"], 0)

    def test_insufficient_cash_resizes_buys(self):
        snapshot = self.snapshot()
        portfolio = ShadowPortfolio(cash=100.0)
        plan = build_frozen_plan(snapshot, portfolio, {s: 100.0 for s in self.histories})
        # The frozen plan must adapt safely if buying power falls before the
        # next open instead of spending the originally observed amount.
        portfolio.cash = 60.0
        observations = {
            symbol: {"open": 200.0, "volume": 1_000_000.0}
            for symbol in ("QUAL", "VLUE")
        }
        result = execute_shadow_plan(plan, portfolio, observations)
        self.assertGreaterEqual(result["ending_cash"], -1e-9)
        self.assertEqual(2, len(result["fills"]))
        self.assertTrue(any(row["partial"] for row in result["fills"]))

    def test_quote_evidence_is_diagnostic_and_execution_reconciles(self):
        plan = {
            "plan_sha256": "plan", "orders": [
                {"symbol": "SPY", "notional": 5_000.0, "side": "buy"}
            ]
        }
        starting = ShadowPortfolio(cash=10_000.0)
        ending = ShadowPortfolio(cash=starting.cash)
        bars = {"SPY": {"open": 100.0, "close": 102.0, "volume": 1_000_000.0}}
        outcome = execute_shadow_plan(plan, ending, bars)
        quotes = opening_quote_diagnostics(
            bars, {"SPY": {"bid": 99.9, "ask": 100.1,
                            "observed_at": "2026-09-28T13:30:01Z"}}
        )
        self.assertFalse(quotes[0]["used_for_execution"])
        self.assertAlmostEqual(100.0, quotes[0]["midpoint"])
        reconciliation = execution_reconciliation(
            plan=plan, outcome=outcome, starting_portfolio=starting,
            ending_portfolio=ending, prior_closes={"SPY": 99.0}, bars=bars,
        )
        self.assertGreater(reconciliation["modeled_execution_cost"], 0)
        self.assertGreater(reconciliation["intraday_holding_pnl"], 0)
        self.assertTrue(reconciliation["invariants"]["portfolio_value_identity_at_open"])
        self.assertEqual(0, reconciliation["paper_orders_submitted"])

    def test_invalid_opening_quote_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "invalid opening quote spread"):
            opening_quote_diagnostics(
                {"SPY": {"open": 100.0}}, {"SPY": {"bid": 101.0, "ask": 100.0}}
            )

    def test_portfolio_checkpoint_and_journal_survive_restart(self):
        folder = Path(".test-prospective-shadow") / uuid.uuid4().hex
        folder.mkdir(parents=True)
        try:
            state_path, journal_path = folder / "state.json", folder / "journal.jsonl"
            portfolio = ShadowPortfolio(25.0, {"SPY": 1.5}, {"old-plan"})
            save_portfolio(portfolio, state_path)
            restored = load_portfolio(state_path)
            self.assertEqual(portfolio, restored)
            outcome = {"status": "executed", "plan_sha256": "new-plan", "fills": []}
            self.assertEqual("appended", append_execution_journal(outcome, journal_path)["status"])
            self.assertEqual("unchanged", append_execution_journal(outcome, journal_path)["status"])
            changed = dict(outcome, fills=[{"symbol": "SPY"}])
            with self.assertRaisesRegex(ValueError, "different outcome"):
                append_execution_journal(changed, journal_path)
        finally:
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()

    def test_compact_bundle_is_immutable_and_retention_is_bounded(self):
        class Store:
            durable = True
            def __init__(self): self.keys = []; self.deleted = []
            def upload_file_if_missing(self, path, key):
                if key not in self.keys: self.keys.append(key)
                return "s3://bucket/" + key
            def list_keys(self, prefix): return sorted(self.keys)
            def delete_file(self, key): self.deleted.append(key); self.keys.remove(key)

        folder = Path(".test-prospective-shadow") / uuid.uuid4().hex
        folder.mkdir(parents=True); store = Store()
        try:
            total_bytes = 0
            for index in range(95):
                session = f"synthetic-{index:03d}"
                result = persist_compact_session_bundle(
                    session=session,
                    strategy_records=[{"strategy": "SPY_BUY_HOLD", "value": 1}],
                    store=store, local_root=folder, retention_sessions=90,
                )
                self.assertLess(result["bytes"], 256 * 1024)
                total_bytes += result["bytes"]
            self.assertEqual(90, len(store.keys))
            self.assertEqual(5, len(store.deleted))
            self.assertLess(total_bytes, 95 * 256 * 1024)
        finally:
            for path in folder.iterdir(): path.unlink()
            folder.rmdir()


if __name__ == "__main__":
    unittest.main()
