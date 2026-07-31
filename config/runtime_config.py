# config/runtime_config.py

from core.state import app_state

# These get populated during lifespan() startup
EMAIL_ADDRESS = None
EMAIL_PASSWORD = None
EMAIL_RECIPIENTS = []

TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID = []

API_KEY = None
SECRET_KEY = None
ALPACA_URL = None

HEALTH_USERNAME = None
HEALTH_PASSWORD = None

PRICE_WINDOW = 200
VOLUME_WINDOW = 200

ENABLE_DEV_ROUTES = False

OLD_STREAM_STRATEGY_ENABLED = False

LAYER4_EXECUTION_ENABLED = False
LAYER3_MARKET_HOURS_ONLY = True

LAYER3_BOOTSTRAP_CONFIRMATION_ENABLED = True
LAYER3_BOOTSTRAP_MIN_BAR_COUNT = 8
FAIL_SAFE_REENTRY_COOLDOWN_SECONDS = 3600

def get_config(key):
    return app_state["config_overrides"].get(key, app_state["config_defaults"].get(key))

def set_config(key, value):
    if key not in app_state["config_defaults"]:
        raise ValueError(f"Invalid config key: {key}")
    app_state["config_overrides"][key] = value

def reset_config(key=None):
    if key:
        app_state["config_overrides"].pop(key, None)
    else:
        app_state["config_overrides"].clear()

