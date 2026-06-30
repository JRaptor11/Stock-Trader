# config_loader.py

import logging
import os

from core.state import app_state
from config import runtime_config as config

from config.constants import (
    TRADE_WINDOW,
    TRADE_LIMIT,
    TRADE_COOLDOWN,
    EQUITY_THRESHOLD,
    EQUITY_FAILSAFE_COOLDOWN,
    MAX_POSITION_LOSS_PERCENT,
    MAX_EQUITY_LOSS,
    MAX_POSITION_LOSS,
    MAX_CONNECTION_ERRORS,
    CONNECTION_COOLDOWN,
    BUY_ORDER_THROTTLE_SECONDS,
    MIN_ORDER_AGE_SECONDS,
    TRADE_RATE_RESPONSE,
    MIN_REENTRY_CHANGE_PCT,
    BUY_CONFIDENCE_THRESHOLD,
    SELL_CONFIDENCE_THRESHOLD,
    CONFIDENCE_CONFLICT_MARGIN,
    MEMORY_ALERT_MB,
    CPU_ALERT_PERCENT,
    TRADE_SUMMARY_FILE,
    TRADE_HISTORY_FILE,
)

from config.app_config import (
    PROGRAM_STARTUP_FILE,
    PROGRAM_SHUTDOWN_FILE,
    PROGRAM_SHUTDOWN_REASON_FILE,
)


def get_int_env(name: str, default: int) -> int:
    """
    Parse integer environment variables safely.
    """
    raw = os.getenv(name)

    if raw is None:
        return default

    try:
        return int(raw.strip())
    except ValueError:
        logging.warning(
            "Invalid integer env var %s=%r. Using default=%s.",
            name,
            raw,
            default,
        )
        return default


def get_float_env(name: str, default: float) -> float:
    """
    Parse float environment variables safely.
    """
    raw = os.getenv(name)

    if raw is None:
        return default

    try:
        return float(raw.strip())
    except ValueError:
        logging.warning(
            "Invalid float env var %s=%r. Using default=%s.",
            name,
            raw,
            default,
        )
        return default


def get_bool_env(name: str, default: bool = False) -> bool:
    """
    Parse boolean environment variables safely.

    Accepted truthy values:
    - true
    - 1
    - yes
    - on
    """
    raw = os.getenv(name)

    if raw is None:
        return default

    return raw.strip().lower() in {"true", "1", "yes", "on"}


def load_environment_config() -> None:
    """
    Load environment variables into the shared config module.
    """

    config.API_KEY = os.getenv("API_KEY")
    config.SECRET_KEY = os.getenv("SECRET_KEY")
    config.ALPACA_URL = os.getenv("ALPACA_URL")

    config.EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
    config.EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
    config.EMAIL_RECIPIENTS = [
        e.strip()
        for e in os.getenv("EMAIL_RECIPIENTS", "").split(",")
        if e.strip()
    ]

    config.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

    if not config.TELEGRAM_BOT_TOKEN:
        logging.error("❌ TELEGRAM_BOT_TOKEN is not set!")

    chat_ids_str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if chat_ids_str:
        try:
            config.TELEGRAM_CHAT_ID = [
                int(cid.strip())
                for cid in chat_ids_str.split(",")
                if cid.strip()
            ]
            logging.info("✅ Loaded Telegram chat IDs: %s", config.TELEGRAM_CHAT_ID)
        except ValueError:
            config.TELEGRAM_CHAT_ID = []
            raise RuntimeError(
                "TELEGRAM_CHAT_ID must be an integer or comma-separated integers "
                "(e.g., 123 or 123,456)."
            )
    else:
        config.TELEGRAM_CHAT_ID = []

    if config.TELEGRAM_BOT_TOKEN and not config.TELEGRAM_CHAT_ID:
        logging.warning(
            "⚠️ TELEGRAM_BOT_TOKEN is set but TELEGRAM_CHAT_ID is missing/empty."
        )

    config.HEALTH_USERNAME = os.getenv("HEALTH_USERNAME")
    config.HEALTH_PASSWORD = os.getenv("HEALTH_PASSWORD")

    if not config.HEALTH_USERNAME or not config.HEALTH_PASSWORD:
        raise RuntimeError("HEALTH_USERNAME / HEALTH_PASSWORD must be set")

    # Keep config in sync for diagnostics/runtime checks.
    # Router mounting itself is decided earlier at import time.
    config.ENABLE_DEV_ROUTES = get_bool_env("ENABLE_DEV_ROUTES", False)

    config.OLD_STREAM_STRATEGY_ENABLED = get_bool_env(
        "OLD_STREAM_STRATEGY_ENABLED",
        False,
    )

    config.LAYER4_EXECUTION_ENABLED = get_bool_env(
        "LAYER4_EXECUTION_ENABLED",
        False,
    )

    config.LAYER3_MARKET_HOURS_ONLY = get_bool_env(
        "LAYER3_MARKET_HOURS_ONLY",
        True,
    )

    config.LAYER3_BOOTSTRAP_CONFIRMATION_ENABLED = get_bool_env(
        "LAYER3_BOOTSTRAP_CONFIRMATION_ENABLED",
        True,
    )

    config.LAYER3_BOOTSTRAP_MIN_BAR_COUNT = get_int_env(
        "LAYER3_BOOTSTRAP_MIN_BAR_COUNT",
        8,
    )

    config.LAYER_MONITOR_RUN_24_7 = get_bool_env(
        "LAYER_MONITOR_RUN_24_7",
        True,
    )

    config.BAR_FRESHNESS_MARKET_HOURS_ONLY = get_bool_env(
        "BAR_FRESHNESS_MARKET_HOURS_ONLY",
        True,
    )

    config.BAR_FRESHNESS_MAX_AGE_MINUTES = get_float_env(
        "BAR_FRESHNESS_MAX_AGE_MINUTES",
        35.0,
    )

    config.BAR_FRESHNESS_MIN_FRESH_SYMBOLS = get_int_env(
        "BAR_FRESHNESS_MIN_FRESH_SYMBOLS",
        5,
    )

    config.BAR_FRESHNESS_MIN_FRESH_RATIO = get_float_env(
        "BAR_FRESHNESS_MIN_FRESH_RATIO",
        0.70,
    )


def apply_runtime_config_to_app_state() -> None:
    """
    Apply loaded environment/config values into app_state.

    This should run during lifespan startup after load_environment_config().
    """

    # Keep the legacy tick strategy observation-only by default.
    # Market-data collection and signal calculation remain active.
    app_state["execution"]["old_stream_strategy_enabled"] = (
        config.OLD_STREAM_STRATEGY_ENABLED
    )

    app_state["execution"]["layer4_execution_enabled"] = (
        config.LAYER4_EXECUTION_ENABLED
    )

    app_state["execution"]["layer3_market_hours_only"] = (
        config.LAYER3_MARKET_HOURS_ONLY
    )

    app_state["execution"]["layer3_bootstrap_confirmation_enabled"] = (
        config.LAYER3_BOOTSTRAP_CONFIRMATION_ENABLED
    )

    app_state["execution"]["layer3_bootstrap_min_bar_count"] = (
        config.LAYER3_BOOTSTRAP_MIN_BAR_COUNT
    )

    symbol_raw = os.environ.get("SYMBOL", "AAPL").strip()
    symbols = [s.strip() for s in symbol_raw.split(",") if s.strip()]
    app_state["main"]["symbol"] = symbols

    if not app_state["main"]["symbol"]:
        raise RuntimeError("Symbol is not configured. Bot cannot proceed.")

    app_state["paths"] = {
        "TRADE_SUMMARY_FILE": TRADE_SUMMARY_FILE,
        "TRADE_HISTORY_FILE": TRADE_HISTORY_FILE,
        "PROGRAM_STARTUP_FILE": PROGRAM_STARTUP_FILE,
        "PROGRAM_SHUTDOWN_FILE": PROGRAM_SHUTDOWN_FILE,
        "PROGRAM_SHUTDOWN_REASON_FILE": PROGRAM_SHUTDOWN_REASON_FILE,
    }

    app_state["config_defaults"] = {
        "TRADE_WINDOW": TRADE_WINDOW,
        "TRADE_LIMIT": TRADE_LIMIT,
        "TRADE_COOLDOWN": TRADE_COOLDOWN,
        "BUY_ORDER_THROTTLE_SECONDS": BUY_ORDER_THROTTLE_SECONDS,
        "MIN_ORDER_AGE_SECONDS": MIN_ORDER_AGE_SECONDS,
        "TRADE_RATE_RESPONSE": TRADE_RATE_RESPONSE,
        "MIN_REENTRY_CHANGE_PCT": MIN_REENTRY_CHANGE_PCT,
        "BUY_CONFIDENCE_THRESHOLD": BUY_CONFIDENCE_THRESHOLD,
        "SELL_CONFIDENCE_THRESHOLD": SELL_CONFIDENCE_THRESHOLD,
        "CONFIDENCE_CONFLICT_MARGIN": CONFIDENCE_CONFLICT_MARGIN,
        "EQUITY_THRESHOLD": EQUITY_THRESHOLD,
        "EQUITY_FAILSAFE_COOLDOWN": EQUITY_FAILSAFE_COOLDOWN,
        "MAX_POSITION_LOSS_PERCENT": MAX_POSITION_LOSS_PERCENT,
        "MAX_EQUITY_LOSS": MAX_EQUITY_LOSS,
        "MAX_POSITION_LOSS": MAX_POSITION_LOSS,
        "MAX_CONNECTION_ERRORS": MAX_CONNECTION_ERRORS,
        "CONNECTION_COOLDOWN": CONNECTION_COOLDOWN,
        "MEMORY_ALERT_MB": MEMORY_ALERT_MB,
        "CPU_ALERT_PERCENT": CPU_ALERT_PERCENT,
        "HEALTH_USERNAME": config.HEALTH_USERNAME,
        "HEALTH_PASSWORD": config.HEALTH_PASSWORD,
    }

    app_state["fail_safes"]["state"] = False

    app_state["main"]["services"] = {
        "EMAIL_RECIPIENTS": config.EMAIL_RECIPIENTS,
        "ALPACA_URL": config.ALPACA_URL,
        "TELEGRAM_BOT_TOKEN": config.TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": config.TELEGRAM_CHAT_ID,
    }


def log_runtime_config_status() -> None:
    """
    Log startup execution/config status.
    """

    logging.info(
        "[LayerConfig] layer4_execution_enabled=%s layer3_market_hours_only=%s "
        "layer_monitor_run_24_7=%s bar_freshness_market_hours_only=%s "
        "bar_freshness_max_age_minutes=%s bar_freshness_min_fresh_symbols=%s "
        "bar_freshness_min_fresh_ratio=%s bootstrap_confirmation_enabled=%s "
        "bootstrap_min_bar_count=%s",
        config.LAYER4_EXECUTION_ENABLED,
        config.LAYER3_MARKET_HOURS_ONLY,
        config.LAYER_MONITOR_RUN_24_7,
        config.BAR_FRESHNESS_MARKET_HOURS_ONLY,
        config.BAR_FRESHNESS_MAX_AGE_MINUTES,
        config.BAR_FRESHNESS_MIN_FRESH_SYMBOLS,
        config.BAR_FRESHNESS_MIN_FRESH_RATIO,
        config.LAYER3_BOOTSTRAP_CONFIRMATION_ENABLED,
        config.LAYER3_BOOTSTRAP_MIN_BAR_COUNT,
    )

    if config.OLD_STREAM_STRATEGY_ENABLED:
        logging.warning(
            "[ExecutionMode] Legacy tick-by-tick stream execution is ENABLED."
        )
    else:
        logging.info(
            "[ExecutionMode] Legacy tick-by-tick stream execution is disabled; "
            "tick tracking remains active."
        )

    if config.LAYER4_EXECUTION_ENABLED:
        logging.warning(
            "[Layer4Execution] Layer 4 paper order execution is ENABLED."
        )
    else:
        logging.info(
            "[Layer4Execution] Layer 4 execution is disabled; dry-run planning only."
        )

    app_state["execution"]["layer_monitor_run_24_7"] = (
        config.LAYER_MONITOR_RUN_24_7
    )

    app_state["execution"]["bar_freshness_market_hours_only"] = (
        config.BAR_FRESHNESS_MARKET_HOURS_ONLY
    )

    app_state["execution"]["bar_freshness_max_age_minutes"] = (
        config.BAR_FRESHNESS_MAX_AGE_MINUTES
    )

    app_state["execution"]["bar_freshness_min_fresh_symbols"] = (
        config.BAR_FRESHNESS_MIN_FRESH_SYMBOLS
    )

    app_state["execution"]["bar_freshness_min_fresh_ratio"] = (
        config.BAR_FRESHNESS_MIN_FRESH_RATIO
    )

    logging.info("ENABLE_DEV_ROUTES resolved to: %s", config.ENABLE_DEV_ROUTES)