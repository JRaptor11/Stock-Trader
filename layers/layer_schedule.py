import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta


def normalize_layer_interval_seconds(interval_seconds: int) -> int:
    try:
        interval_seconds = int(interval_seconds)
    except Exception:
        interval_seconds = 600

    if interval_seconds <= 0:
        return 600

    if 3600 % interval_seconds != 0:
        logging.warning(
            "[Layers] interval_seconds=%s does not divide evenly into 1 hour. "
            "Defaulting to 600 seconds for wall-clock alignment.",
            interval_seconds,
        )
        return 600

    return interval_seconds


def next_wall_clock_run_time(
    interval_seconds: int,
    now: datetime | None = None,
) -> datetime:
    interval_seconds = normalize_layer_interval_seconds(interval_seconds)

    now = now or datetime.now(timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    hour_start = now.replace(minute=0, second=0, microsecond=0)

    seconds_since_hour = (
        now.minute * 60
        + now.second
        + now.microsecond / 1_000_000
    )

    next_slot_seconds = (
        math.floor(seconds_since_hour / interval_seconds) + 1
    ) * interval_seconds

    if next_slot_seconds >= 3600:
        return hour_start + timedelta(hours=1)

    return hour_start + timedelta(seconds=next_slot_seconds)


async def sleep_until_next_layer_boundary(
    *,
    shutdown_event,
    interval_seconds: int,
    min_spacing_seconds: float = 180.0,
) -> None:
    interval_seconds = normalize_layer_interval_seconds(interval_seconds)

    next_run_at = next_wall_clock_run_time(interval_seconds)
    now = datetime.now(timezone.utc)
    sleep_seconds = max(0.0, (next_run_at - now).total_seconds())

    if sleep_seconds <= min_spacing_seconds:
        skipped_run_at = next_run_at
        next_run_at = next_run_at + timedelta(seconds=interval_seconds)
        sleep_seconds = max(0.0, (next_run_at - now).total_seconds())

        logging.info(
            "[Layers] Next wall-clock cycle is too close; skipping one boundary | "
            "skipped_run_at=%s next_run_at=%s sleep_seconds=%.1f "
            "min_spacing_seconds=%.1f interval_seconds=%s",
            skipped_run_at.isoformat(),
            next_run_at.isoformat(),
            sleep_seconds,
            min_spacing_seconds,
            interval_seconds,
        )

    logging.info(
        "[Layers] Sleeping until next wall-clock cycle | next_run_at=%s "
        "sleep_seconds=%.1f interval_seconds=%s",
        next_run_at.isoformat(),
        sleep_seconds,
        interval_seconds,
    )

    try:
        await asyncio.wait_for(
            asyncio.to_thread(shutdown_event.wait, sleep_seconds),
            timeout=sleep_seconds + 5,
        )
    except asyncio.TimeoutError:
        pass