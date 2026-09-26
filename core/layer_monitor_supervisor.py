"""Liveness supervision for the live Layer monitor task."""

import asyncio
import logging
from datetime import datetime, timezone

from core.state import app_state


def _parse_utc_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


async def run_layer_monitor_supervisor(
    monitor_factory,
    alert_sender,
    *,
    interval_seconds: int = 300,
    check_seconds: float = 30.0,
    stall_seconds: float | None = None,
    restart_delay_base_seconds: float = 5.0,
) -> None:
    """Keep the Layer monitor alive and replace a dead or stalled child task."""
    stall_seconds = float(stall_seconds or max(900, interval_seconds * 3))
    shutdown_event = app_state["stream"]["shutdown_event"]
    monitor_state = app_state.setdefault("layers", {}).setdefault("monitor", {})

    while not shutdown_event.is_set():
        child = asyncio.create_task(
            monitor_factory(interval_seconds=interval_seconds),
            name="layer-monitor-task",
        )
        app_state["main"]["layer_monitor_task"] = child
        restart_reason = None

        try:
            while not shutdown_event.is_set():
                done, _ = await asyncio.wait({child}, timeout=check_seconds)
                if done:
                    try:
                        child.result()
                        restart_reason = "unexpected_clean_exit"
                    except asyncio.CancelledError:
                        if shutdown_event.is_set():
                            return
                        restart_reason = "unexpected_cancellation"
                    except Exception as exc:
                        restart_reason = f"task_exception:{type(exc).__name__}:{exc}"
                    break

                heartbeat = _parse_utc_timestamp(monitor_state.get("heartbeat_at"))
                run_24_7 = bool(
                    app_state.get("execution", {}).get("layer_monitor_run_24_7", True)
                )
                market_is_open = bool(
                    app_state.get("layers", {}).get("rebalance", {}).get("market_is_open", False)
                )
                should_be_advancing = run_24_7 or market_is_open
                age_seconds = (
                    (datetime.now(timezone.utc) - heartbeat).total_seconds()
                    if heartbeat else None
                )
                if should_be_advancing and (
                    heartbeat is None or age_seconds > stall_seconds
                ):
                    restart_reason = (
                        "missing_heartbeat" if heartbeat is None
                        else f"stale_heartbeat:{age_seconds:.1f}s"
                    )
                    child.cancel()
                    await asyncio.gather(child, return_exceptions=True)
                    break
        except asyncio.CancelledError:
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
            raise

        if shutdown_event.is_set():
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
            break

        now_iso = datetime.now(timezone.utc).isoformat()
        restart_count = int(monitor_state.get("restart_count") or 0) + 1
        monitor_state.update({
            "status": "restarting",
            "heartbeat_at": now_iso,
            "last_phase": "supervisor_restart",
            "restart_count": restart_count,
            "last_restart_at": now_iso,
            "last_restart_reason": restart_reason,
            "last_error": restart_reason,
        })
        logging.error(
            "[Layers] Restarting Layer monitor | reason=%s restart_count=%s",
            restart_reason,
            restart_count,
        )
        # Email remains available after Render's ephemeral disk is replaced.
        if restart_count == 1 or restart_count & (restart_count - 1) == 0:
            await alert_sender(
                "Layer Monitor Automatically Restarted",
                f"Reason: {restart_reason}\nRestart count: {restart_count}\nTime: {now_iso}",
            )
        await asyncio.sleep(min(60.0, restart_delay_base_seconds * restart_count))

    monitor_state.update({
        "status": "stopped",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "last_phase": "supervisor_stopped",
    })
