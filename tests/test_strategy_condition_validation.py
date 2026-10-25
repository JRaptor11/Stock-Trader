import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from research.strategy_condition_validation import validate_bundle, write_validation


def _csv(rows):
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows); return output.getvalue()


class StrategyConditionValidationTests(unittest.TestCase):
    @staticmethod
    def _episode_bundle(path, *, mutate_from=None, omit_index=None):
        dates = [(date(2026, 1, 1) + timedelta(days=index)).isoformat()
                 for index in range(48)]
        conditions = []
        daily = []
        for index, day in enumerate(dates):
            bucket = "UP" if (index // 4) % 2 == 0 else "DOWN"
            conditions.append({"date": day, "regime_bucket": bucket})
            phase = .001 if index % 2 else -.001
            values = {
                "SPY_BUY_HOLD": 0.0,
                "UP_WINNER": (.012 + phase) if bucket == "UP" else (.002 + phase / 2),
                "DOWN_WINNER": (.002 + phase / 2) if bucket == "UP" else (.012 + phase),
            }
            if mutate_from is not None and day >= mutate_from:
                values["UP_WINNER"] = -.75 if index % 2 else .75
                values["DOWN_WINNER"] = .80 if index % 2 else -.80
            if index == omit_index:
                continue
            for strategy, value in values.items():
                daily.append({
                    "source": "nested", "strategy": strategy, "date": day,
                    "daily_return": value,
                })
        with zipfile.ZipFile(path, "w") as bundle:
            bundle.writestr("normalized_daily_returns.csv", _csv(daily))
            bundle.writestr("causal_market_conditions.csv", _csv(conditions))
            bundle.writestr("strategy_evidence_manifest.json", "{}")
        return dates

    def test_training_qualification_never_uses_test_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.zip"
            dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(12)]
            conditions = [{"date": day, "volatility_20d_bucket": "Q5_HIGH"} for day in dates]
            daily = []
            for strategy, values in (("SPY_BUY_HOLD", [0] * 12), ("TEST", [.02] * 6 + [-.50] * 6)):
                daily.extend({"source": "test", "hypothesis_id": "TEST", "trial_id": "one", "strategy": strategy, "date": day, "cost_bps": 10, "daily_return": value} for day, value in zip(dates, values))
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", _csv(daily)); bundle.writestr("causal_market_conditions.csv", _csv(conditions)); bundle.writestr("strategy_evidence_manifest.json", json.dumps({}))
            first, _, _ = validate_bundle(path, train_sessions=6, test_sessions=3, step_sessions=3, minimum_bucket_sessions=3, alpha=1)
            qualification = first[0]["qualified_on_train"]
            daily[-1]["daily_return"] = .99
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", _csv(daily)); bundle.writestr("causal_market_conditions.csv", _csv(conditions)); bundle.writestr("strategy_evidence_manifest.json", json.dumps({}))
            second, _, _ = validate_bundle(path, train_sessions=6, test_sessions=3, step_sessions=3, minimum_bucket_sessions=3, alpha=1)
            self.assertEqual(qualification, second[0]["qualified_on_train"])

    def test_requires_enough_chronological_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.zip"
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", "source,strategy,date,daily_return\nx,SPY_BUY_HOLD,2026-01-01,0\n")
                bundle.writestr("causal_market_conditions.csv", "date,volatility_20d_bucket\n2026-01-01,Q1_LOW\n")
                bundle.writestr("strategy_evidence_manifest.json", "{}")
            with self.assertRaisesRegex(ValueError, "insufficient"):
                validate_bundle(path, train_sessions=2, test_sessions=1)

    def test_focused_declaration_limits_trials_and_labels_reused_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); evidence = root / "evidence.zip"; declaration = root / "declaration.json"
            dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(12)]
            conditions = [{"date": day, "volatility_20d_bucket": "Q1_LOW"} for day in dates]
            daily = [{"source": "x", "strategy": strategy, "date": day, "daily_return": value}
                     for strategy, value in (("SPY_BUY_HOLD", 0), ("TEST", .01)) for day in dates]
            with zipfile.ZipFile(evidence, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", _csv(daily)); bundle.writestr("causal_market_conditions.csv", _csv(conditions)); bundle.writestr("strategy_evidence_manifest.json", "{}")
            declaration.write_text(json.dumps({"frozen_at": "2026-12-31T00:00:00Z", "evidence_observed_through": "2026-12-31", "hypotheses": [{"id": "one", "cohort": "test", "strategy": "TEST", "dimension": "volatility_20d_bucket", "bucket": "Q1_LOW"}]}))
            rows, _summary, metadata = validate_bundle(evidence, train_sessions=6, test_sessions=3, step_sessions=3, minimum_bucket_sessions=3, alpha=1, declaration_path=declaration, cohort="test")
            self.assertEqual(1, metadata["family_trials"])
            self.assertTrue(all(row["hypothesis_id"] == "one" for row in rows))
            self.assertTrue(all(row["evidence_class"] == "retrospective_reuse" for row in rows))

    def test_focused_declaration_rejects_unknown_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); declaration = root / "declaration.json"
            declaration.write_text(json.dumps({"frozen_at": "2026-01-01", "evidence_observed_through": "2025-01-01", "hypotheses": [{"id": "bad", "cohort": "test", "strategy": "MISSING", "dimension": "volatility_20d_bucket", "bucket": "Q1_LOW"}]}))
            with self.assertRaisesRegex(ValueError, "no hypotheses for cohort other"):
                from research.strategy_condition_validation import _load_declaration
                _load_declaration(declaration, "other")

    def test_explicit_dimensions_define_family_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.zip"
            dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(12)]
            conditions = [{"date": day, "volatility_change_5d_bucket": "Q5_HIGH", "unrelated_bucket": "Q1_LOW"} for day in dates]
            daily = [{"source": "x", "strategy": strategy, "date": day, "daily_return": value}
                     for strategy, value in (("SPY_BUY_HOLD", 0), ("TEST", .01)) for day in dates]
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", _csv(daily)); bundle.writestr("causal_market_conditions.csv", _csv(conditions)); bundle.writestr("strategy_evidence_manifest.json", "{}")
            rows, _summary, metadata = validate_bundle(
                path, train_sessions=6, test_sessions=3, minimum_bucket_sessions=3, alpha=1,
                dimensions=("volatility_change_5d_bucket",),
            )
            self.assertEqual(["volatility_change_5d_bucket"], metadata["condition_dimensions"])
            self.assertEqual(5, metadata["family_trials"])
            self.assertTrue(all(row["dimension"] == "volatility_change_5d_bucket" for row in rows))
            with self.assertRaisesRegex(ValueError, "unavailable"):
                validate_bundle(path, train_sessions=6, test_sessions=3, dimensions=("missing_bucket",))

    def test_fixed_strategy_can_be_the_direct_benchmark(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.zip"
            dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(12)]
            conditions = [{"date": day, "transition_bucket": "Q3"} for day in dates]
            daily = []
            for strategy, value in (("SPY_BUY_HOLD", 0), ("STATIC_MULTI_SLEEVE", .005), ("TEST", .01)):
                daily.extend({"source": "x", "strategy": strategy, "date": day, "daily_return": value} for day in dates)
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", _csv(daily)); bundle.writestr("causal_market_conditions.csv", _csv(conditions)); bundle.writestr("strategy_evidence_manifest.json", "{}")
            rows, _summary, metadata = validate_bundle(
                path, train_sessions=6, test_sessions=3, minimum_bucket_sessions=3, alpha=1,
                benchmark_strategy="STATIC_MULTI_SLEEVE",
            )
            test_row = next(row for row in rows if row["strategy"] == "TEST")
            self.assertEqual("STATIC_MULTI_SLEEVE", metadata["benchmark_strategy"])
            self.assertEqual("STATIC_MULTI_SLEEVE", test_row["benchmark_strategy"])
            self.assertGreater(test_row["test_excess_return"], 0)
            self.assertNotIn("STATIC_MULTI_SLEEVE", {row["strategy"] for row in rows})

    def test_focused_declaration_benchmark_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            declaration = Path(directory) / "declaration.json"
            declaration.write_text(json.dumps({
                "frozen_at": "2026-01-01", "evidence_observed_through": "2025-01-01",
                "benchmark_strategy": "STATIC_MULTI_SLEEVE",
                "hypotheses": [{"id": "one", "strategy": "TEST", "dimension": "transition_bucket", "bucket": "Q3"}],
            }))
            with self.assertRaisesRegex(ValueError, "benchmark does not match"):
                validate_bundle(Path("unused.zip"), declaration_path=declaration)

    def test_nested_walk_forward_keeps_whole_episodes_and_selects_per_bucket(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "episodes.zip"
            self._episode_bundle(evidence)
            rows, summary, metadata = validate_bundle(
                evidence,
                train_sessions=20,
                test_sessions=8,
                step_sessions=4,
                minimum_bucket_sessions=4,
                alpha=1.0,
                dimensions=("regime_bucket",),
                validation_mode="nested_episode_grouped",
                nested_inner_folds=8,
                nested_max_selected_per_dimension=1,
            )
            self.assertEqual("nested_episode_grouped", metadata["validation_mode"])
            self.assertIn("whole inner-validation episode", metadata["inner_selection_policy"])

            test_ids_by_fold = {}
            for row in rows:
                train_ids = set(json.loads(row["train_episode_ids"]))
                test_ids = set(json.loads(row["test_episode_ids"]))
                inner_ids = set(json.loads(row["inner_validation_episode_ids"]))
                self.assertTrue(train_ids.isdisjoint(test_ids))
                self.assertTrue(inner_ids.issubset(train_ids))
                test_ids_by_fold.setdefault(row["fold"], test_ids)
                self.assertEqual("condition_bucket_within_dimension", row["selection_scope"])
            folds = sorted(test_ids_by_fold)
            for index, fold in enumerate(folds):
                for later in folds[index + 1:]:
                    self.assertTrue(
                        test_ids_by_fold[fold].isdisjoint(test_ids_by_fold[later])
                    )

            first_fold = [row for row in rows if row["fold"] == folds[0]]
            selected = {
                (row["bucket"], row["strategy"])
                for row in first_fold if row["selected_on_train"]
            }
            self.assertEqual(
                {("UP", "UP_WINNER"), ("DOWN", "DOWN_WINNER")}, selected,
            )
            self.assertTrue(all(not item["router_eligible"] for item in summary))

    def test_nested_first_fold_selection_is_invariant_to_outer_test_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "episodes.zip"
            dates = self._episode_bundle(evidence)
            kwargs = dict(
                train_sessions=20,
                test_sessions=8,
                step_sessions=8,
                minimum_bucket_sessions=4,
                alpha=1.0,
                dimensions=("regime_bucket",),
                validation_mode="nested_episode_grouped",
                nested_inner_folds=8,
            )
            before, _, _ = validate_bundle(evidence, **kwargs)
            first_fold = min(row["fold"] for row in before)
            signature_before = sorted(
                (row["strategy"], row["bucket"], row["selected_on_train"],
                 row["inner_validation_episode_ids"])
                for row in before if row["fold"] == first_fold
            )
            self._episode_bundle(evidence, mutate_from=dates[20])
            after, _, _ = validate_bundle(evidence, **kwargs)
            signature_after = sorted(
                (row["strategy"], row["bucket"], row["selected_on_train"],
                 row["inner_validation_episode_ids"])
                for row in after if row["fold"] == first_fold
            )
            self.assertEqual(signature_before, signature_after)

    def test_nested_excludes_a_partially_observed_condition_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "missing-session.zip"
            self._episode_bundle(evidence, omit_index=10)
            rows, _summary, metadata = validate_bundle(
                evidence,
                train_sessions=20,
                test_sessions=8,
                step_sessions=8,
                minimum_bucket_sessions=4,
                alpha=1.0,
                dimensions=("regime_bucket",),
                validation_mode="nested_episode_grouped",
                nested_inner_folds=8,
            )
            excluded = metadata["incomplete_condition_episode_ids_excluded"][
                "regime_bucket"
            ]
            self.assertEqual(1, len(excluded))
            excluded_id = excluded[0]
            self.assertIn(":3:", excluded_id)
            for row in rows:
                self.assertNotIn(excluded_id, json.loads(row["train_episode_ids"]))
                self.assertNotIn(excluded_id, json.loads(row["test_episode_ids"]))

    def test_multiple_testing_report_requires_an_explicit_complete_family(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "family.zip"
            dates = [(date(2026, 1, 1) + timedelta(days=index)).isoformat()
                     for index in range(40)]
            conditions = [{"date": day, "regime_bucket": "UP"} for day in dates]
            daily = []
            for index, day in enumerate(dates):
                for strategy, value in (
                    ("SPY_BUY_HOLD", 0.0),
                    ("FIRST", .009 + (.002 if index % 2 else -.002)),
                    ("SECOND", .003 + (.002 if index % 3 else -.002)),
                ):
                    daily.append({
                        "source": "family", "strategy": strategy, "date": day,
                        "daily_return": value,
                    })
            with zipfile.ZipFile(evidence, "w") as bundle:
                bundle.writestr("normalized_daily_returns.csv", _csv(daily))
                bundle.writestr("causal_market_conditions.csv", _csv(conditions))
                bundle.writestr("strategy_evidence_manifest.json", "{}")

            _rows_without, _summary, absent = validate_bundle(
                evidence, train_sessions=20, test_sessions=10,
                dimensions=("regime_bucket",),
            )
            self.assertEqual(
                "complete_search_family_not_supplied",
                absent["unconditional_multiple_testing_report"]["reason"],
            )
            _rows_with, _summary, computed = validate_bundle(
                evidence, train_sessions=20, test_sessions=10,
                dimensions=("regime_bucket",),
                multiple_testing_family_trials=2,
                multiple_testing_cscv_partitions=4,
                multiple_testing_bootstrap_samples=19,
            )
            report = computed["unconditional_multiple_testing_report"]
            self.assertEqual("computed", report["status"])
            self.assertEqual("caller_asserted_complete_family_count",
                             report["family_count_source"])
            self.assertEqual(2, report["declared_family_trials"])
            self.assertIn("deflated_sharpe", report)
            self.assertFalse(report["router_or_promotion_eligible"])

            from research.experiment_audit import append_job_trials
            trial_ledger = Path(directory) / "trials.jsonl"
            append_job_trials(trial_ledger, {
                "job_id": "family-job",
                "engine": "tier1_etf_daily",
                "experiment": {
                    "hypothesis_id": "VALIDATION_FAMILY",
                    "trial_id": "validation-family-001",
                    "tested_variants": [
                        {"variant_id": "first", "strategy_id": "FIRST"},
                        {"variant_id": "second", "strategy_id": "SECOND"},
                    ],
                },
            }, accepted_at="2026-01-01T00:00:00+00:00")
            _ledger_rows, _summary, ledger_backed = validate_bundle(
                evidence, train_sessions=20, test_sessions=10,
                dimensions=("regime_bucket",),
                trial_ledger_path=trial_ledger,
                trial_family_id="validation_family",
                multiple_testing_cscv_partitions=4,
                multiple_testing_bootstrap_samples=19,
            )
            ledger_report = ledger_backed["unconditional_multiple_testing_report"]
            self.assertEqual("computed", ledger_report["status"])
            self.assertEqual(
                "tamper_evident_trial_ledger", ledger_report["family_count_source"],
            )

            artifact = Path(directory) / "validation.zip"
            artifact_metadata = write_validation(
                evidence, artifact,
                train_sessions=20,
                test_sessions=10,
                dimensions=("regime_bucket",),
                multiple_testing_family_trials=2,
                multiple_testing_cscv_partitions=4,
                multiple_testing_bootstrap_samples=19,
            )
            with zipfile.ZipFile(artifact) as bundle:
                self.assertIn(
                    "unconditional_multiple_testing_report.json", bundle.namelist(),
                )
                archived_report = json.loads(
                    bundle.read("unconditional_multiple_testing_report.json")
                )
            self.assertEqual(
                artifact_metadata["unconditional_multiple_testing_report"],
                archived_report,
            )

            _rows_mismatch, _summary, mismatch = validate_bundle(
                evidence, train_sessions=20, test_sessions=10,
                dimensions=("regime_bucket",),
                multiple_testing_family_trials=3,
            )
            self.assertEqual(
                "complete_family_count_does_not_match_aligned_return_series",
                mismatch["unconditional_multiple_testing_report"]["reason"],
            )


if __name__ == "__main__":
    unittest.main()
