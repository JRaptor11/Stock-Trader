"""Async orchestration helpers for synchronous SDK work."""

from __future__ import annotations

import asyncio


async def run_sync_with_heartbeat(
    function,
    /,
    *args,
    heartbeat_callback=None,
    heartbeat_interval_seconds: float = 30.0,
    **kwargs,
):
    """Run blocking work in a thread while renewing a liveness heartbeat."""
    heartbeat_interval_seconds = max(
        0.01,
        float(heartbeat_interval_seconds),
    )
    task = asyncio.create_task(
        asyncio.to_thread(function, *args, **kwargs)
    )

    try:
        while True:
            if heartbeat_callback is not None:
                heartbeat_callback()

            done, _ = await asyncio.wait(
                {task},
                timeout=heartbeat_interval_seconds,
            )
            if task in done:
                return task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise
