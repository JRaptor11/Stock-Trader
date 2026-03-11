import os
import time
import logging
import psutil

from state import app_state
from utils.alerts_utils import send_email_alert


def monitor_system_resources(interval: int = 60) -> None:
    """
    Periodically logs memory/CPU usage.
    Exits cleanly when app_state["stream"]["shutdown_event"] is set.
    """
    process = psutil.Process(os.getpid())

    # Default thresholds (falls back to your constants if you want)
    mem_alert_mb = 500
    cpu_alert_percent = 80

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
            mem_mb = process.memory_info().rss / (1024 * 1024)
            cpu_percent = process.cpu_percent(interval=1)

            logging.info(f"[MONITOR] Memory: {mem_mb:.2f} MB | CPU: {cpu_percent:.2f}%")

            if mem_mb > mem_alert_mb:
                send_email_alert("⚠️ High Memory Usage", f"Memory usage at {mem_mb:.2f} MB.")
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