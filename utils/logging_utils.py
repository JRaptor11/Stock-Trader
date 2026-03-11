import os
import logging
import asyncio
from logging.handlers import RotatingFileHandler
from .alerts_utils import send_email_alert

def configure_logging():
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    json_logs = os.getenv("JSON_LOGS", "false").lower() == "true"
    run_as_web = os.getenv("RUN_AS_WEB", "false").lower() == "true"
    websocket_log_level = os.getenv("WEBSOCKET_LOG_LEVEL", "WARNING").upper()

    os.makedirs("logs", exist_ok=True)
    log_file = os.path.join("logs", "trading_bot.log")

    log_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] [%(threadName)-12s] [%(name)-20s] %(message)s",
        "%Y-%m-%d %H:%M:%S"
    )
    json_formatter = logging.Formatter(
        '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "thread": "%(threadName)s", "module": "%(name)s", "message": "%(message)s"}',
        "%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(log_formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(json_formatter if json_logs else log_formatter)

    logging.basicConfig(level=log_level, handlers=[file_handler, console_handler], force=True)

    for lib, level in {
        "urllib3": "WARNING",
        "websockets": "WARNING",
        "alpaca": "INFO",
        "asyncio": "WARNING",
        "httpcore": "WARNING",            # 🔇 Suppress low-level HTTP debug logs
        "httpx": "WARNING",               # 🔇 Suppress request/response logs
        "telegram": "WARNING",            # 🔇 Suppress general Telegram logs
        "telegram.ext": "WARNING",        # 🔇 Suppress polling updates like "no new updates"
    }.items():
        logging.getLogger(lib).setLevel(level)

    apply_websocket_logging(websocket_log_level)
    logging.getLogger().addFilter(_critical_alert_filter)

    if run_as_web:
        uvicorn_access = logging.getLogger("uvicorn.access")
        uvicorn_access.handlers.clear()
        uvicorn_access.propagate = False

    logging.info(f"✅ Logging configured. Level: {log_level}, File: {log_file}, WebSocket Level: {websocket_log_level}")

def apply_websocket_logging(level_str):
    levels = {
        "CRITICAL": logging.CRITICAL,
        "ERROR": logging.ERROR,
        "WARNING": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
    }
    level = levels.get(level_str, logging.WARNING)
    logging.getLogger("websockets.protocol").setLevel(level)
    logging.getLogger("websockets.client").setLevel(level)

def _critical_alert_filter(record):
    if record.levelno >= logging.CRITICAL:
        send_email_alert("CRITICAL ERROR", f"CRITICAL ERROR: {record.getMessage()}")
    return True

def handle_asyncio_exception(loop, context):
    logging.error(f"⚠️ Global asyncio error: {context.get('message')}")
    exception = context.get("exception")
    if exception:
        logging.error("Exception:", exc_info=exception)
