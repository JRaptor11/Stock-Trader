import gc
import os
import time
import logging
from typing import Any
import psutil

from core.state import app_state
from integrations.alerts import send_email_alert

MB = 1024 * 1024

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)

def _trim_process_memory() -> None:
    """Release unreachable objects and, on Linux, return free heap pages."""
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (ImportError, OSError, AttributeError):
        pass

def _safe_len(value: Any) -> int | None:
    try:
        return len(value)
    except (TypeError, AttributeError):
        return None

def resource_snapshot(process: psutil.Process | None = None) -> dict:
    """Return a compact, JSON-safe snapshot for production diagnostics."""
    process = process or psutil.Process(os.getpid())
    memory = process.memory_info()
    layers = app_state.get("layers", {})
    buffer = app_state.get("market_data", {}).get("buffer")
    market = buffer.snapshot() if buffer is not None else {}
    return {
        "rss_mb": round(memory.rss / MB, 2),
        "vms_mb": round(memory.vms / MB, 2),
        "python_gc_counts": list(gc.get_count()),
        "open_trades_count": _safe_len(app_state.get("open_trades", {})),
        "execution_plan_history_count": _safe_len(layers.get("execution_plan_history", [])),
        "layer_state_sizes": {
            key: _safe_len(value) for key, value in layers.items()
            if _safe_len(value) is not None
        },
        "market_data": {
            symbol: {
                "ticks": values.get("tick_count", 0),
                "live_1m_bars": values.get("live_1m_bar_count", 0),
                "live_5m_bars": values.get("live_5m_bar_count", 0),
            }
            for symbol, values in market.items()
        },
    }


def monitor_system_resources(interval: int = 60) -> None:
    """
    Periodically logs memory/CPU usage.
    Exits cleanly when app_state["stream"]["shutdown_event"] is set.
    """
    process = psutil.Process(os.getpid())

    mem_alert_mb = _env_float("MEMORY_ALERT_MB", 500)
    mem_recovery_mb = _env_float("MEMORY_RECOVERY_MB", mem_alert_mb - 50)
    alert_cooldown_seconds = _env_float("RESOURCE_ALERT_COOLDOWN_SECONDS", 1800)
    cpu_alert_percent = _env_float("CPU_ALERT_PERCENT", 80)
    memory_alert_active = False
    last_memory_alert_at = 0.0

    shutdown_event = app_state["stream"].get("shutdown_event")
    if shutdown_event is None:
        # Fallback: behave like old code
        shutdown_event = None

    while True:
        # Exit ASAP if shutdown requested
        if shutdown_event and shutdown_event.is_set():
            logging.info("[ResourceMonitor] ✅ Exiting due to shutdown_event.")
            return

        try:
            mem_mb = process.memory_info().rss / MB
            cpu_percent = process.cpu_percent(interval=1)

            if mem_mb > mem_alert_mb:
                before_trim_mb = mem_mb
                _trim_process_memory()
                mem_mb = process.memory_info().rss / MB
                logging.info(
                    "[ResourceMonitor] Memory reclaim: %.2f MB -> %.2f MB",
                    before_trim_mb, mem_mb,
                )

            logging.info("[MONITOR] Memory: %.2f MB | CPU: %.2f%%", mem_mb, cpu_percent)
            now = time.monotonic()

            if mem_mb > mem_alert_mb:
                if not memory_alert_active or now - last_memory_alert_at >= alert_cooldown_seconds:
                    send_email_alert(
                        "⚠️ High Memory Usage",
                        f"Memory usage at {mem_mb:.2f} MB after reclamation.",
                    )
                    last_memory_alert_at = now
                memory_alert_active = True
            elif memory_alert_active and mem_mb <= mem_recovery_mb:
                send_email_alert(
                    "✅ Memory Usage Recovered",
                    f"Memory usage recovered to {mem_mb:.2f} MB.",
                )
                memory_alert_active = False
                last_memory_alert_at = 0.0
            if cpu_percent > cpu_alert_percent:
                send_email_alert("⚠️ High CPU Usage", f"CPU usage at {cpu_percent:.2f}%.")

            # Replace time.sleep(...) with an interruptible wait
            # Wait (interval - 1) seconds, but exit early on shutdown.
            wait_s = max(0.0, float(interval) - 1.0)
            if shutdown_event:
                shutdown_event.wait(timeout=wait_s)
            else:
                time.sleep(wait_s)

        except Exception as e:
            logging.error(f"[ResourceMonitor] System monitor error: {e}")
            # Still allow shutdown to interrupt the “cooldown”
            if shutdown_event:
                shutdown_event.wait(timeout=float(interval))
            else:
                time.sleep(interval)
