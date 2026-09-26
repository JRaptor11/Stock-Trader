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
    execution_attribution,
    freeze_decision,
    load_portfolio,
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


if __name__ == "__main__":
    unittest.main()
