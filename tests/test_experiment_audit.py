import json
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from research.experiment_audit import (
    AuditIntegrityError,
    HoldoutReuseError,
    assert_audit_anchors,
    audit_integrity_summary,
    append_holdout_consumption,
    append_job_trials,
    holdout_consumption_from_job,
    register_job_audit,
    trial_family_summary,
)


def _process_append_trial(payload):
    path, index = payload
    append_job_trials(Path(path), {
        "job_id": f"process-job-{index:02d}",
        "engine": "tier1_etf_daily",
        "bars_csv": "bars.csv",
        "experiment": {
            "hypothesis_id": "PROCESS_LOCK_TEST",
            "trial_id": f"process-trial-{index:02d}",
            "tested_variants": [
                {"variant_id": "one", "strategy": "ONE"},
                {"variant_id": "two", "strategy": "TWO"},
            ],
        },
        "tier1_config": {"primary_cost_bps": 10},
    })


class ExperimentAuditTests(unittest.TestCase):
    def _job(self):
        return {
            "job_id": "literature-momentum-001",
            "engine": "tier1_etf_daily",
            "bars_csv": "bars.csv",
            "experiment": {
                "hypothesis_id": "LIT_MOMENTUM_UP_LOW_VOL_001",
                "trial_id": "locked-primary",
                "tested_variants": [
                    {"variant_id": "relative-momentum", "strategy": "RELATIVE_MOMENTUM"},
                    {"variant_id": "factor-momentum", "strategy": "FACTOR_MOMENTUM"},
                ],
            },
            "tier1_config": {"primary_cost_bps": 10, "cost_ladder_bps": [1, 5, 10, 20]},
        }

    def test_trial_ledger_is_hash_chained_idempotent_and_counts_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.jsonl"
            first = append_job_trials(path, self._job(), accepted_at="2026-10-11T00:00:00+00:00")
            second = append_job_trials(path, self._job(), accepted_at="2026-10-12T00:00:00+00:00")
            self.assertEqual(2, first["new_records"])
            self.assertEqual("unchanged", second["status"])
            summary = trial_family_summary(path)
            family = summary["families"]["LIT_MOMENTUM_UP_LOW_VOL_001"]
            self.assertEqual(2, family["distinct_variant_count"])
            self.assertFalse(family["variant_count_is_lower_bound"])

    def test_unenumerated_job_is_reported_as_a_lower_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.jsonl"
            append_job_trials(path, {
                "job_id": "legacy", "bars_csv": "legacy.csv",
                "experiment": {"hypothesis_id": "LEGACY"},
            })
            family = trial_family_summary(path)["families"]["LEGACY"]
            self.assertTrue(family["variant_count_is_lower_bound"])
            self.assertEqual(["legacy"], family["unenumerated_job_ids"])

    def test_ledger_keeps_hashes_not_repeated_large_job_definitions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.jsonl"
            job = self._job()
            job["tier1_config"]["large_research_note"] = "x" * 100_000
            result = append_job_trials(path, job)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(2, len(rows))
            self.assertLess(path.stat().st_size, 5_000)
            self.assertNotIn("variant_definition", rows[0])
            self.assertEqual(result["job_definition_sha256"], rows[0]["job_definition_sha256"])

    def test_trial_identity_reuse_and_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.jsonl"
            job = self._job()
            append_job_trials(path, job)
            changed = json.loads(json.dumps(job))
            changed["tier1_config"]["primary_cost_bps"] = 20
            with self.assertRaisesRegex(AuditIntegrityError, "different definition"):
                append_job_trials(path, changed)
            rows = path.read_text(encoding="utf-8").splitlines()
            tampered = json.loads(rows[0]); tampered["engine"] = "changed"
            rows[0] = json.dumps(tampered)
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AuditIntegrityError, "row hash"):
                trial_family_summary(path)

    def test_holdout_consumption_rejects_overlap_and_allows_exact_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holdouts.jsonl"
            first = {
                "holdout_id": "modern-2024-2026", "consumer_id": "study-001",
                "dataset_id": "alpaca-iex-adjusted-daily", "universe_id": "tier2-etfs",
                "frequency": "1d", "start_date": "2024-01-01", "end_date": "2026-09-30",
                "purpose": "locked external validation",
            }
            self.assertEqual("appended", append_holdout_consumption(path, first)["status"])
            self.assertEqual("unchanged", append_holdout_consumption(path, first)["status"])
            overlap = {**first, "holdout_id": "renamed", "consumer_id": "study-002",
                       "dataset_id": "ALPACA-IEX-ADJUSTED-DAILY",
                       "universe_id": "TIER2-ETFS",
                       "start_date": "2026-01-01", "end_date": "2026-10-01"}
            with self.assertRaisesRegex(HoldoutReuseError, "study-001"):
                append_holdout_consumption(path, overlap)
            independent = {**first, "holdout_id": "prehistory", "consumer_id": "study-002",
                           "start_date": "2008-01-01", "end_date": "2010-12-31"}
            self.assertEqual("appended", append_holdout_consumption(path, independent)["status"])

    def test_only_structured_holdout_declaration_is_consumed(self):
        prose_only = {"experiment": {"holdout_policy": "never touch this sample"}}
        self.assertIsNone(holdout_consumption_from_job(prose_only))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = self._job()
            job["experiment"]["holdout_policy"] = "free form legacy text"
            result = register_job_audit(
                job=job, trial_ledger=root / "trials.jsonl",
                holdout_registry=root / "holdouts.jsonl",
            )
            self.assertEqual("not_declared", result["holdout"]["status"])
            self.assertFalse((root / "holdouts.jsonl").exists())

    def test_structured_holdout_schema_is_strict(self):
        job = self._job()
        job["experiment"]["holdout_consumption"] = {
            "holdout_id": "one", "consumer_id": "study", "dataset_id": "dataset",
            "universe_id": "universe", "frequency": "1d",
            "start_date": "2026-02-01", "end_date": "2026-01-01",
            "purpose": "validation",
        }
        with self.assertRaisesRegex(ValueError, "precedes"):
            holdout_consumption_from_job(job)

        job["experiment"]["holdout_consumption"] = {
            "holdout_id": "one", "consumer_id": "study", "dataset_id": "dataset",
            "universe_id": "universe", "frequency": "1d",
            "start_date": "2026-01-01", "end_date": "2026-02-01",
            "purpose": "validation", "typo_field": True,
        }
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            holdout_consumption_from_job(job)

    def test_registration_validates_trials_before_consuming_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = self._job()
            job["experiment"]["tested_variants"].append(
                dict(job["experiment"]["tested_variants"][0])
            )
            job["experiment"]["holdout_consumption"] = {
                "holdout_id": "one", "consumer_id": "study", "dataset_id": "dataset",
                "universe_id": "universe", "frequency": "1d",
                "start_date": "2026-01-01", "end_date": "2026-02-01",
                "purpose": "validation",
            }
            with self.assertRaisesRegex(ValueError, "duplicate tested variant"):
                register_job_audit(
                    job=job, trial_ledger=root / "trials.jsonl",
                    holdout_registry=root / "holdouts.jsonl",
                )
            self.assertFalse((root / "trials.jsonl").exists())
            self.assertFalse((root / "holdouts.jsonl").exists())

    def test_atomic_replace_failure_preserves_the_previous_valid_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.jsonl"
            append_job_trials(path, self._job())
            original = path.read_bytes()
            next_job = self._job()
            next_job["job_id"] = "literature-momentum-002"
            next_job["experiment"]["trial_id"] = "locked-secondary"
            with patch("research.experiment_audit.os.replace", side_effect=OSError("stop")):
                with self.assertRaisesRegex(OSError, "stop"):
                    append_job_trials(path, next_job)
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(2, trial_family_summary(path)["accepted_trial_records"])
            self.assertFalse(list(path.parent.glob(".*.tmp")))

    def test_concurrent_writers_do_not_lose_chain_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.jsonl"

            def append(index):
                job = self._job()
                job["job_id"] = f"job-{index:02d}"
                job["experiment"]["trial_id"] = f"trial-{index:02d}"
                return append_job_trials(path, job)

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(append, range(20)))
            summary = trial_family_summary(path)
            self.assertEqual(40, summary["accepted_trial_records"])
            self.assertEqual(2, summary["families"]["LIT_MOMENTUM_UP_LOW_VOL_001"]["distinct_variant_count"])

    def test_cross_process_writers_do_not_lose_chain_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.jsonl"
            context = multiprocessing.get_context("spawn")
            with context.Pool(processes=4) as pool:
                pool.map(_process_append_trial, [(str(path), index) for index in range(8)])
            summary = trial_family_summary(path)
            self.assertEqual(16, summary["accepted_trial_records"])
            self.assertEqual(
                2, summary["families"]["PROCESS_LOCK_TEST"]["distinct_variant_count"],
            )

    def test_incomplete_final_row_and_naive_timestamp_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.jsonl"
            append_job_trials(path, self._job())
            path.write_bytes(path.read_bytes().rstrip(b"\n"))
            with self.assertRaisesRegex(AuditIntegrityError, "incomplete final row"):
                trial_family_summary(path)
        with self.assertRaisesRegex(ValueError, "timezone"):
            append_job_trials("unused.jsonl", self._job(), accepted_at="2026-10-11T00:00:00")

    def test_integrity_summary_validates_both_chains(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            register_job_audit(
                job=self._job(), trial_ledger=root / "trials.jsonl",
                holdout_registry=root / "holdouts.jsonl",
            )
            summary = audit_integrity_summary(
                trial_ledger=root / "trials.jsonl",
                holdout_registry=root / "holdouts.jsonl",
            )
            self.assertEqual(2, summary["trials"]["accepted_trial_records"])
            self.assertEqual(0, summary["holdouts"]["consumed_holdouts"])

    def test_durable_anchor_rejects_a_valid_but_truncated_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial_path = root / "trials.jsonl"
            holdout_path = root / "holdouts.jsonl"
            first = self._job()
            first_result = register_job_audit(
                job=first, trial_ledger=trial_path,
                holdout_registry=holdout_path,
            )
            prefix = trial_path.read_bytes()
            second = self._job()
            second["job_id"] = "literature-momentum-002"
            second["experiment"]["trial_id"] = "locked-secondary"
            second_result = register_job_audit(
                job=second, trial_ledger=trial_path,
                holdout_registry=holdout_path,
            )
            self.assertNotEqual(
                first_result["trials"]["chain_sha256"],
                second_result["trials"]["chain_sha256"],
            )
            # The prefix is internally valid; only the durable acceptance
            # anchor proves that later records have disappeared.
            trial_path.write_bytes(prefix)
            trial_family_summary(trial_path)
            with self.assertRaisesRegex(AuditIntegrityError, "rollback or truncation"):
                assert_audit_anchors(
                    trial_ledger=trial_path,
                    holdout_registry=holdout_path,
                    required_trial_anchors=[
                        second_result["trials"]["chain_sha256"],
                    ],
                )


if __name__ == "__main__":
    unittest.main()
