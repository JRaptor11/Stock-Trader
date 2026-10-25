import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from research.market_conditions import (
    LITERATURE_CROSS_ASSET_SYMBOLS,
    LITERATURE_EQUITY_BREADTH_SYMBOLS,
    causal_market_conditions,
    literature_momentum_state,
    literature_panic_rebound_hazard,
)
from research.market_state_episodes import causal_state_labels
from research.strategy_condition_validation import _load_declaration
from research.strategy_registry import HYPOTHESES, validate_experiment_declaration
from research.tier1_etf_replay import (
    CROSS_ASSET_RISK,
    Tier1Config,
    _strategy_rebalance_frequency,
    _simulate,
    _targets,
    _validated_calendar,
    config_from_job,
)
from research.universes import resolve_universe


ROOT = Path(__file__).resolve().parents[1]


def _price_path(returns):
    values = [100.0]
    for value in returns:
        values.append(values[-1] * (1.0 + value))
    return values


def _up_low_relative_vol_path():
    old = [0.02 if index % 2 == 0 else -0.015 for index in range(504)]
    recent = [0.0005] * 252
    return _price_path(old + recent)


def _up_high_relative_vol_path():
    old = [0.0005] * 504
    recent = [0.03 if index % 2 == 0 else -0.025 for index in range(252)]
    return _price_path(old + recent)


class LiteratureRegimeStageATests(unittest.TestCase):
    @staticmethod
    def _diagnostic_bars(session_count=820):
        symbols = tuple(dict.fromkeys((
            *resolve_universe("ETF_LONG_TERM_STATE_CANONICAL"),
            *LITERATURE_CROSS_ASSET_SYMBOLS,
        )))
        dates = []
        cursor = date(2020, 1, 2)
        while len(dates) < session_count:
            if cursor.weekday() < 5:
                dates.append(cursor.isoformat())
            cursor += timedelta(days=1)
        bars = {}
        for index, day in enumerate(dates):
            bars[day] = {}
            market_move = 0.0003 + 0.0015 * ((index % 17) - 8) / 8
            for offset, symbol in enumerate(symbols):
                # Shared movement plus a deterministic asset-specific cycle
                # produces finite, non-identical correlations without random
                # input or any dependence on future sessions.
                prior = (
                    bars[dates[index - 1]][symbol]["close"]
                    if index else 100.0 + offset
                )
                asset_move = market_move + 0.0004 * (((index + offset * 3) % 11) - 5) / 5
                close = prior * (1.0 + asset_move)
                bars[day][symbol] = {
                    "open": prior,
                    "high": max(prior, close),
                    "low": min(prior, close),
                    "close": close,
                }
        return dates, bars

    def test_exact_momentum_state_uses_36m_return_and_relative_12m_volatility(self):
        self.assertEqual(
            "UP_LOW_RELATIVE_VOL",
            literature_momentum_state(_up_low_relative_vol_path()),
        )
        self.assertEqual(
            "UP_HIGH_RELATIVE_VOL",
            literature_momentum_state(_up_high_relative_vol_path()),
        )
        self.assertIsNone(literature_momentum_state([100.0] * 756))

    def test_panic_rebound_hazard_is_causal_and_requires_breadth_confirmation(self):
        spy = [100.0] * 50 + [99.0, 97.0, 94.0, 91.0, 88.0, 84.0, 82.0, 85.0, 89.0, 94.0, 100.0]
        recovering_sector = [100.0] * 50 + [99.0, 98.0, 97.0, 96.0, 95.0, 94.0, 95.0, 98.0, 102.0, 106.0, 110.0]
        histories = {"SPY": spy}
        histories.update({symbol: list(recovering_sector) for symbol in LITERATURE_EQUITY_BREADTH_SYMBOLS})
        self.assertTrue(literature_panic_rebound_hazard(histories))

        # Changing data that would only arrive after this decision cannot be
        # part of the already-computed signal.
        extended = {symbol: values + [values[-1] * 0.5] for symbol, values in histories.items()}
        self.assertTrue(literature_panic_rebound_hazard(histories))
        self.assertNotEqual(
            literature_panic_rebound_hazard(histories),
            literature_panic_rebound_hazard(extended),
        )

    def test_up_low_vol_filter_and_panic_guard_wrap_the_unchanged_momentum_target(self):
        config = Tier1Config(
            universe_name="ETF_LONG_TERM_CROSS_ASSET_HISTORY",
            strategy_names=(
                "LITERATURE_UP_LOW_VOL_RELATIVE_MOMENTUM",
                "LITERATURE_PANIC_REBOUND_GUARDED_MOMENTUM",
            ),
        )
        histories = {
            symbol: _price_path([0.0001 + offset * 0.00003] * 756)
            for offset, symbol in enumerate(resolve_universe(config.universe_name))
        }
        histories["SPY"] = _up_low_relative_vol_path()
        favorable = _targets("LITERATURE_UP_LOW_VOL_RELATIVE_MOMENTUM", histories, config)
        self.assertNotEqual({"SHY": 1.0}, favorable)
        self.assertAlmostEqual(1.0, sum(favorable.values()))

        histories["SPY"] = _up_high_relative_vol_path()
        self.assertEqual(
            {"SHY": 1.0},
            _targets("LITERATURE_UP_LOW_VOL_RELATIVE_MOMENTUM", histories, config),
        )

        with patch(
            "research.tier1_etf_replay.literature_panic_rebound_hazard",
            return_value=True,
        ):
            self.assertEqual(
                {"SHY": 1.0},
                _targets("LITERATURE_PANIC_REBOUND_GUARDED_MOMENTUM", histories, config),
            )

    def test_long_flat_trend_preserves_fixed_slots_and_leaves_inactive_slots_in_shy(self):
        config = Tier1Config(
            universe_name="ETF_LONG_TERM_CROSS_ASSET_HISTORY",
            strategy_names=("LITERATURE_12M_LONG_FLAT_TREND",),
        )
        histories = {
            symbol: _price_path(([0.001] if symbol in {"SPY", "QQQ"} else [-0.0005]) * 252)
            for symbol in resolve_universe(config.universe_name)
        }
        targets = _targets("LITERATURE_12M_LONG_FLAT_TREND", histories, config)
        slot = 1.0 / len(CROSS_ASSET_RISK)
        self.assertAlmostEqual(slot, targets["SPY"])
        self.assertAlmostEqual(slot, targets["QQQ"])
        self.assertAlmostEqual(1.0 - 2.0 * slot, targets["SHY"])
        self.assertAlmostEqual(1.0, sum(targets.values()))

    def test_stage_a_files_are_frozen_retrospective_jobs_not_generation_015(self):
        declaration = json.loads((ROOT / "research" / "literature-regime-replication-stage-a-001.json").read_text())
        self.assertEqual("frozen_before_return_analysis", declaration["status"])
        self.assertEqual("paused_and_unchanged", declaration["generation_015_status"])
        self.assertEqual("2026-10-09", declaration["holdout_policy"]["consumed_through"])
        self.assertFalse(declaration["holdout_policy"]["historical_folds_are_untouched_holdout"])
        self.assertIn("LITERATURE_REGIME_REPLICATION_STAGE_A_001", HYPOTHESES)

        for suffix in ("cross-asset", "quality"):
            job = json.loads((ROOT / "research" / f"literature-regime-replication-stage-a-001-{suffix}-job.json").read_text())
            config = config_from_job(job)
            self.assertIsNone(config.holdout_start_date)
            self.assertEqual("paused", job["research_evaluation"]["generation_015"])
            self.assertNotIn("holdout_consumption", job["experiment"])
            self.assertEqual(
                "retrospective_consumed",
                job["experiment"]["evidence_status"]["classification"],
            )
            self.assertFalse(
                job["experiment"]["evidence_status"]["untouched_holdout_consumed"]
            )
            variants = job["experiment"]["tested_variants"]
            self.assertEqual(
                set(config.strategy_names),
                {variant["strategy"] for variant in variants},
            )
            self.assertEqual(len(variants), len({variant["variant_id"] for variant in variants}))
            self.assertTrue(all(variant.get("role") for variant in variants))
            self.assertTrue(all(variant.get("frozen_definition") for variant in variants))
            self.assertEqual(
                "LITERATURE_REGIME_REPLICATION_STAGE_A_001",
                validate_experiment_declaration(job["experiment"])["hypothesis_id"],
            )

    def test_focused_declaration_is_executable_and_cohort_scoped(self):
        path = ROOT / "research" / "literature-regime-replication-stage-a-001.json"
        cross_asset = _load_declaration(path, "cross_asset_mechanisms")
        quality = _load_declaration(path, "quality_proxy")
        self.assertEqual("SPY_BUY_HOLD", cross_asset["benchmark_strategy"])
        self.assertEqual(
            {
                "A1_MOMENTUM_UP_LOW_RELATIVE_VOL",
                "A2_GUARDED_MOMENTUM_HAZARD",
                "A3_TREND_HIGH_DIRECTIONAL_MAGNITUDE",
                "A4_TREND_LOW_CROSS_ASSET_CORRELATION",
            },
            {item["id"] for item in cross_asset["hypotheses"]},
        )
        self.assertEqual(
            {"A5_QUALITY_PROXY_DOWN_HIGH_RELATIVE_VOL"},
            {item["id"] for item in quality["hypotheses"]},
        )
        self.assertTrue(all(
            item["cohort"] == cross_asset["selected_cohort"]
            for item in cross_asset["hypotheses"]
        ))

    def test_cross_asset_trend_diagnostics_are_causal_and_do_not_change_state_labels(self):
        dates, bars = self._diagnostic_bars()
        state_symbols = resolve_universe("ETF_LONG_TERM_STATE_CANONICAL")
        canonical = causal_market_conditions(dates, bars, state_symbols)
        diagnosed = causal_market_conditions(
            dates, bars, state_symbols,
            diagnostic_symbols=LITERATURE_CROSS_ASSET_SYMBOLS,
        )
        self.assertEqual(causal_state_labels(canonical), causal_state_labels(diagnosed))
        final = diagnosed[dates[-1]]
        self.assertIsNotNone(final["cross_asset_directional_magnitude_252d"])
        self.assertIsNotNone(final["cross_asset_correlation_60d"])
        self.assertIn(
            final["cross_asset_directional_magnitude_252d_bucket"],
            {"Q1_LOW", "Q2", "Q3", "Q4", "Q5_HIGH"},
        )
        self.assertIn(
            final["cross_asset_correlation_60d_bucket"],
            {"Q1_LOW", "Q2", "Q3", "Q4", "Q5_HIGH"},
        )

        cutoff = dates[-20]
        mutated = {
            day: {symbol: dict(row) for symbol, row in day_rows.items()}
            for day, day_rows in bars.items()
        }
        for day in dates[-19:]:
            for symbol in LITERATURE_CROSS_ASSET_SYMBOLS:
                mutated[day][symbol]["close"] *= 3.0
        mutated_conditions = causal_market_conditions(
            dates, mutated, state_symbols,
            diagnostic_symbols=LITERATURE_CROSS_ASSET_SYMBOLS,
        )
        fields = (
            "cross_asset_directional_magnitude_252d",
            "cross_asset_directional_magnitude_252d_bucket",
            "cross_asset_correlation_60d",
            "cross_asset_correlation_60d_bucket",
        )
        for day in dates:
            if day > cutoff or day not in diagnosed:
                continue
            self.assertEqual(
                {field: diagnosed[day].get(field) for field in fields},
                {field: mutated_conditions[day].get(field) for field in fields},
            )

    def test_quality_job_minimum_scored_sessions_is_feasible_on_its_frozen_span(self):
        job = json.loads(
            (ROOT / "research" / "literature-regime-replication-stage-a-001-quality-job.json").read_text()
        )
        config = config_from_job(job)
        symbols = resolve_universe(config.universe_name)
        dates = []
        cursor = date.fromisoformat(config.required_common_start_date)
        end = date(2026, 10, 9)
        weekday_index = 0
        while cursor <= end:
            if cursor.weekday() < 5:
                weekday_index += 1
                # A conservative synthetic calendar removes one weekday in
                # twenty, slightly more than the normal U.S. holiday rate.
                if weekday_index % 20:
                    dates.append(cursor.isoformat())
            cursor += timedelta(days=1)
        bars = {
            day: {
                symbol: {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
                for symbol in symbols
            }
            for day in dates
        }
        scored, report = _validated_calendar(dates, bars, symbols, config)
        self.assertGreaterEqual(report["scored_sessions"], config.minimum_scored_sessions)
        self.assertEqual(report["scored_sessions"], len(scored) - report["warmup_sessions"])

    def test_stage_a_cadences_are_explicit(self):
        config = Tier1Config(rebalance_frequency="monthly")
        self.assertEqual(
            "monthly",
            _strategy_rebalance_frequency("LITERATURE_UP_LOW_VOL_RELATIVE_MOMENTUM", config),
        )
        self.assertEqual(
            "monthly",
            _strategy_rebalance_frequency("LITERATURE_PANIC_REBOUND_GUARDED_MOMENTUM", config),
        )
        self.assertEqual(
            "monthly",
            _strategy_rebalance_frequency("LITERATURE_12M_LONG_FLAT_TREND", config),
        )

    def test_panic_guard_interrupts_monthly_cadence_only_on_state_transitions(self):
        config = Tier1Config(
            universe_name="ETF_LONG_TERM_CROSS_ASSET_HISTORY",
            strategy_names=("LITERATURE_PANIC_REBOUND_GUARDED_MOMENTUM",),
        )
        symbols = resolve_universe(config.universe_name)
        dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
        bars = {
            day: {
                symbol: {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
                for symbol in symbols
            }
            for day in dates
        }
        target_calls = []

        def targets(_name, histories, _config):
            length = len(histories["SPY"])
            target_calls.append(length)
            return {"SHY": 1.0} if length in (3, 4) else {"SPY": 1.0}

        with patch(
            "research.tier1_etf_replay.literature_panic_rebound_hazard",
            side_effect=lambda histories, _benchmark: len(histories["SPY"]) in (3, 4),
        ):
            _simulate(
                "LITERATURE_PANIC_REBOUND_GUARDED_MOMENTUM",
                dates,
                bars,
                config,
                10.0,
                dates[0],
                target_function=targets,
            )
        self.assertEqual([1, 3, 5], target_calls)


if __name__ == "__main__":
    unittest.main()
