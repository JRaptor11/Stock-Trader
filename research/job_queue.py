"""Pure queue policy helpers shared by the hosted research coordinator."""

from __future__ import annotations


DETERMINISTIC_ERROR_TYPES = frozenset({
    "ValueError",
    "TypeError",
    "KeyError",
    "FileNotFoundError",
    "FileExistsError",
    "UnicodeDecodeError",
    "CSVError",
    "PermissionError",
})


def queued_in_fifo_order(payloads: list[dict]) -> list[dict]:
    return sorted(
        (payload for payload in payloads if payload.get("status") == "queued"),
        key=lambda item: (str(item.get("queued_at") or ""), str(item.get("job_id") or "")),
    )


def classify_failure(error_type: str, *, storage_budget_exceeded: bool = False) -> tuple[bool, str]:
    if storage_budget_exceeded:
        return False, "storage_budget"
    if str(error_type) in DETERMINISTIC_ERROR_TYPES:
        return False, "deterministic_input_or_configuration"
    return True, "transient_infrastructure_or_worker"


def retry_delay_seconds(
    attempt_count: int,
    *,
    maximum_retries: int,
    base_seconds: float,
    maximum_seconds: float,
) -> float | None:
    """Return the delay after an attempt, or None when retries are exhausted."""
    attempt_count = max(1, int(attempt_count))
    if attempt_count > max(0, int(maximum_retries)):
        return None
    return min(float(maximum_seconds), float(base_seconds) * (2 ** (attempt_count - 1)))


def progress_aware_restart_state(
    payload: dict, *, maximum_consecutive: int, maximum_lifetime: int,
) -> dict:
    """Renew retry allowance only when a durable checkpoint advanced."""
    baseline = int(payload.get("attempt_started_checkpoint_timestamps") or 0)
    durable = int(payload.get("checkpoint_completed_timestamps") or 0)
    advanced = durable > baseline
    consecutive = 0 if advanced else int(
        payload.get("consecutive_restarts_without_progress") or 0
    ) + 1
    lifetime = int(
        payload.get("lifetime_restart_count")
        or payload.get("lifetime_attempt_count")
        or 0
    ) + 1
    return {
        "checkpoint_advanced_during_attempt": advanced,
        "attempt_started_checkpoint_timestamps": baseline,
        "latest_durable_checkpoint_timestamps": durable,
        "consecutive_restarts_without_progress": consecutive,
        "lifetime_restart_count": lifetime,
        "retry_allowed": (
            consecutive <= max(0, int(maximum_consecutive))
            and lifetime <= max(1, int(maximum_lifetime))
        ),
    }
