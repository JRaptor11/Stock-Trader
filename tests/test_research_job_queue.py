import unittest

from research.job_queue import (
    classify_failure, progress_aware_restart_state, queued_in_fifo_order,
    retry_delay_seconds,
)


class ResearchJobQueuePolicyTests(unittest.TestCase):
    def test_fifo_order_uses_enqueue_time_then_job_id(self):
        payloads = [
            {"job_id": "third", "status": "queued", "queued_at": "2026-08-14T12:02:00+00:00"},
            {"job_id": "second", "status": "queued", "queued_at": "2026-08-14T12:01:00+00:00"},
            {"job_id": "first", "status": "queued", "queued_at": "2026-08-14T12:00:00+00:00"},
            {"job_id": "done", "status": "complete", "queued_at": "2026-08-14T11:00:00+00:00"},
        ]
        self.assertEqual(
            [item["job_id"] for item in queued_in_fifo_order(payloads)],
            ["first", "second", "third"],
        )

    def test_deterministic_failures_do_not_retry(self):
        for error_type in ("ValueError", "FileNotFoundError", "UnicodeDecodeError"):
            with self.subTest(error_type=error_type):
                self.assertEqual(
                    classify_failure(error_type),
                    (False, "deterministic_input_or_configuration"),
                )
        self.assertEqual(
            classify_failure("RuntimeError", storage_budget_exceeded=True),
            (False, "storage_budget"),
        )

    def test_transient_failures_retry_with_bounded_exponential_backoff(self):
        delays = [
            retry_delay_seconds(
                attempt, maximum_retries=3, base_seconds=30, maximum_seconds=90,
            )
            for attempt in range(1, 5)
        ]
        self.assertEqual(delays, [30.0, 60.0, 90.0, None])
        self.assertEqual(
            classify_failure("EndpointConnectionError"),
            (True, "transient_infrastructure_or_worker"),
        )

    def test_durable_progress_renews_consecutive_retry_allowance(self):
        state = progress_aware_restart_state({
            "attempt_started_checkpoint_timestamps": 100,
            "checkpoint_completed_timestamps": 200,
            "consecutive_restarts_without_progress": 3,
            "lifetime_restart_count": 7,
        }, maximum_consecutive=3, maximum_lifetime=20)
        self.assertTrue(state["checkpoint_advanced_during_attempt"])
        self.assertEqual(0, state["consecutive_restarts_without_progress"])
        self.assertEqual(8, state["lifetime_restart_count"])
        self.assertTrue(state["retry_allowed"])

    def test_no_progress_and_lifetime_limits_stop_restart_loop(self):
        no_progress = progress_aware_restart_state({
            "attempt_started_checkpoint_timestamps": 100,
            "checkpoint_completed_timestamps": 100,
            "consecutive_restarts_without_progress": 3,
            "lifetime_restart_count": 4,
        }, maximum_consecutive=3, maximum_lifetime=20)
        self.assertEqual(4, no_progress["consecutive_restarts_without_progress"])
        self.assertFalse(no_progress["retry_allowed"])
        lifetime = progress_aware_restart_state({
            "attempt_started_checkpoint_timestamps": 100,
            "checkpoint_completed_timestamps": 200,
            "lifetime_restart_count": 20,
        }, maximum_consecutive=3, maximum_lifetime=20)
        self.assertFalse(lifetime["retry_allowed"])


if __name__ == "__main__":
    unittest.main()
