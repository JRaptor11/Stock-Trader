import csv
import json
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from research.tier1_etf_replay import (
    LEGACY_STRATEGIES, STRATEGIES, Tier1Config, _causal_market_regimes, _targets, _validated_calendar,
    config_from_job, load_daily_bars,
    run_tier1_job,
)
from research.universes import resolve_universe


def write_bars(path: Path, sessions: int = 280):
    symbols = resolve_universe("ETF_TIER1_RESEARCH")
    day = date(2024, 1, 2); rows = []
    for index in range(sessions):
        while day.weekday() >= 5: day += timedelta(days=1)
        for offset, symbol in enumerate(symbols):
            # Differentiated, deterministic trends make rankings testable.
            close = 100 + index * (0.02 + offset * 0.002)
            rows.append({"timestamp": day.isoformat()+"T21:00:00Z", "symbol": symbol,
                         "open": close-0.05, "high": close+0.1, "low": close-0.1,
                         "close": close, "volume": 1000000, "trade_count": 1000, "vwap": close})
        day += timedelta(days=1)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)


class Tier1ETFReplayTests(unittest.TestCase):
    def test_generation_3_strategies_produce_long_only_normalized_targets(self):
        symbols = resolve_universe("ETF_GENERATION_3")
        histories = {
            symbol: [100 + index * (0.03 + offset * 0.001) for index in range(260)]
            for offset, symbol in enumerate(symbols)
        }
        config = Tier1Config(
            universe_name="ETF_GENERATION_3",
            momentum_lookbacks_days=(63, 126, 252),
        )
        for strategy in (
            "CROSS_ASSET_DUAL_MOMENTUM", "DIVERSIFIED_TREND", "REGIME_BALANCED",
            "SECTOR_ROTATION_CONCENTRATED", "SECTOR_ROTATION_INV_VOL",
            "REGIME_ROUTED_SECTOR",
        ):
            targets = _targets(strategy, histories, config)
            self.assertAlmostEqual(1.0, sum(targets.values()))
            self.assertTrue(all(weight >= 0 for weight in targets.values()))
            self.assertTrue(set(targets) <= set(symbols))

    def test_concentrated_sector_strategy_holds_one_risk_asset(self):
        symbols = resolve_universe("ETF_GENERATION_3")
        histories = {
            symbol: [100 + index * (0.03 + offset * 0.001) for index in range(260)]
            for offset, symbol in enumerate(symbols)
        }
        config = Tier1Config(
            universe_name="ETF_GENERATION_3",
            momentum_lookbacks_days=(63, 126, 252),
        )
        targets = _targets("SECTOR_ROTATION_CONCENTRATED", histories, config)
        self.assertEqual(1, len(targets))
        self.assertAlmostEqual(1.0, sum(targets.values()))

    def test_inverse_vol_sector_strategy_normalizes_positive_weights(self):
        symbols = resolve_universe("ETF_GENERATION_3")
        histories = {
            symbol: [100 + index * (0.02 + offset * 0.001) + ((index % 5) * offset * .002)
                     for index in range(260)]
            for offset, symbol in enumerate(symbols)
        }
        config = Tier1Config(
            universe_name="ETF_GENERATION_3",
            momentum_lookbacks_days=(63, 126, 252),
        )
        targets = _targets("SECTOR_ROTATION_INV_VOL", histories, config)
        self.assertLessEqual(len(targets), config.sector_holdings)
        self.assertAlmostEqual(1.0, sum(targets.values()))
        self.assertTrue(all(weight > 0 for weight in targets.values()))

    def test_regime_router_moves_to_cash_in_bear_high_volatility(self):
        symbols = resolve_universe("ETF_GENERATION_3")
        histories = {symbol: [200.0 - index * .25 for index in range(260)] for symbol in symbols}
        for index in range(200, 260):
            histories["SPY"][index] += 8.0 if index % 2 else -8.0
        config = Tier1Config(
            universe_name="ETF_GENERATION_3",
            momentum_lookbacks_days=(63, 126, 252),
        )
        self.assertEqual({"SHY": 1.0}, _targets("REGIME_ROUTED_SECTOR", histories, config))

    def test_market_regime_for_day_uses_previous_close(self):
        config = Tier1Config(trend_lookback_days=2, volatility_lookback_days=2,
                             regime_high_volatility_annualized=1.0)
        dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
        closes = [100.0, 101.0, 102.0, 80.0]
        bars = {day: {"SPY": {"close": close}} for day, close in zip(dates, closes)}
        regimes = _causal_market_regimes(dates, bars, config)
        self.assertEqual("BULL_LOW_VOL", regimes["2026-01-04"])

    def test_multi_sleeve_strategies_are_long_only_and_normalized(self):
        symbols = resolve_universe("ETF_TIER2_MULTI_SLEEVE")
        histories = {
            symbol: [100 + index * (0.02 + offset * .0005) for index in range(260)]
            for offset, symbol in enumerate(symbols)
        }
        config = Tier1Config(
            universe_name="ETF_TIER2_MULTI_SLEEVE",
            momentum_lookbacks_days=(63, 126, 252),
        )
        for strategy in (
            "FACTOR_ETF_MOMENTUM", "INDUSTRY_ETF_MOMENTUM",
            "STATIC_MULTI_SLEEVE", "REGIME_MULTI_SLEEVE",
        ):
            targets = _targets(strategy, histories, config)
            self.assertAlmostEqual(1.0, sum(targets.values()))
            self.assertTrue(all(weight >= 0 for weight in targets.values()))
            self.assertTrue(set(targets) <= set(symbols))

    def test_config_rejects_unknown_fields(self):
        with self.assertRaisesRegex(ValueError, "unknown tier1_config"):
            config_from_job({"tier1_config": {"future_leak": True}})

    def test_default_roster_preserves_frozen_forward_tournament(self):
        self.assertEqual(LEGACY_STRATEGIES, Tier1Config().strategy_names)
        self.assertNotIn("CROSS_ASSET_DUAL_MOMENTUM", Tier1Config().strategy_names)

    def test_config_rejects_invalid_strategy_roster(self):
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            Tier1Config(strategy_names=())
        with self.assertRaisesRegex(ValueError, "unknown Tier 1 strategies"):
            Tier1Config(strategy_names=("NOT_A_STRATEGY",))
        with self.assertRaisesRegex(ValueError, "duplicates"):
            Tier1Config(strategy_names=("SPY_BUY_HOLD", "SPY_BUY_HOLD"))

    def test_config_requires_complete_nonoverlapping_holdout(self):
        with self.assertRaisesRegex(ValueError, "must be set together"):
            Tier1Config(discovery_end_date="2024-09-02")
        with self.assertRaisesRegex(ValueError, "must precede"):
            Tier1Config(discovery_end_date="2024-09-03", holdout_start_date="2024-09-03")

    def test_coverage_rejects_late_common_start(self):
        config = Tier1Config(required_common_start_date="2024-01-01")
        symbols = resolve_universe(config.universe_name)
        dates = ["2024-02-01", "2024-02-02"]
        bars = {day: {symbol: {"close": 100} for symbol in symbols} for day in dates}
        with self.assertRaisesRegex(ValueError, "begins later than required"):
            _validated_calendar(dates, bars, symbols, config)

    def test_coverage_reports_true_common_start_and_warmup(self):
        config = Tier1Config(momentum_lookbacks_days=(2,), trend_lookback_days=2,
                             volatility_lookback_days=2)
        symbols = resolve_universe(config.universe_name)
        dates = [f"2024-01-{day:02d}" for day in range(1, 7)]
        bars = {day: {symbol: {"close": 100} for symbol in symbols} for day in dates}
        bars[dates[0]].pop(symbols[-1])
        common, report = _validated_calendar(dates, bars, symbols, config)
        self.assertEqual("2024-01-02", report["common_start"])
        self.assertEqual(3, report["warmup_sessions"])
        self.assertEqual("2024-01-05", report["scored_start"])
        self.assertEqual(dates[1:], common)

    def test_daily_loader_aggregates_intraday_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bars.csv"
            path.write_text("timestamp,symbol,open,high,low,close\n2026-01-02T14:30:00Z,SPY,100,102,99,101\n2026-01-02T20:55:00Z,SPY,101,103,100,102\n",encoding="utf-8")
            dates,bars=load_daily_bars(path,{"SPY"})
            self.assertEqual(["2026-01-02"],dates); self.assertEqual(100,bars[dates[0]]["SPY"]["open"]); self.assertEqual(102,bars[dates[0]]["SPY"]["close"])

    def test_tournament_writes_all_costs_strategies_and_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); bars=root/"bars.csv"; archive=root/"result.zip"; write_bars(bars)
            job={"engine":"tier1_etf_daily","experiment":{"hypothesis_id":"ETF_DUAL_MOMENTUM","trial_id":"trial-001"},"tier1_config":{"cost_ladder_bps":[1,10],"primary_cost_bps":10,"discovery_end_date":"2024-09-02","holdout_start_date":"2024-09-03"}}
            run_tier1_job(job,bars,archive,"abc123")
            with zipfile.ZipFile(archive) as bundle:
                names=set(bundle.namelist()); manifest=json.loads(bundle.read("tier1_manifest.json")); summary=json.loads(bundle.read("tier1_summary.json"))
                self.assertIn("tier1_cost_ladder_scorecard.csv",names); self.assertIn("tier1_promotion_gates.csv",names)
                self.assertIn("tier1_period_scorecard.csv",names)
                self.assertIn("tier1_rolling_3y_scorecard.csv",names)
                self.assertIn("tier1_walk_forward_scorecard.csv",names)
                self.assertIn("tier1_regime_scorecard.csv",names)
                self.assertIn("tier1_pairwise_summary.csv",names)
                self.assertIn("warm-up excluded",manifest["execution_semantics"])
                self.assertEqual(253,manifest["coverage"]["warmup_sessions"])
                self.assertEqual(set(LEGACY_STRATEGIES),{row["strategy"] for row in summary["scorecards"]})
                self.assertEqual("holdout",summary["promotion_period"])
                self.assertTrue(all(not row["paper_trading_approved"] for row in summary["promotion_gates"]))


if __name__ == "__main__": unittest.main()
