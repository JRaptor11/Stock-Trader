import asyncio
import threading
import logging
import inspect
from .alerts_utils import send_email_alert

def safe_thread(target, name=None, daemon=True):
    def wrapper():
        try:
            if inspect.iscoroutinefunction(target):
                asyncio.run(target())
            else:
                target()
        except Exception as e:
            thread_name = name or getattr(target, '__name__', 'unknown')
            logging.exception(f"Unhandled exception in thread '{thread_name}': {e}")
            try:
                send_email_alert("🚨 Thread Crashed", f"Thread '{thread_name}' crashed:\n{e}")
            except Exception:
                logging.warning("Failed sending thread crash email (ignored).", exc_info=True)

    thread = threading.Thread(target=wrapper, name=name, daemon=daemon)
    thread.start()
    return thread
