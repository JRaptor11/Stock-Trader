# app_config_init.py

import logging
import os

from utils import config_utils as config


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

    config.LAYER3_EXECUTION_ENABLED = get_bool_env(
        "LAYER3_EXECUTION_ENABLED",
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