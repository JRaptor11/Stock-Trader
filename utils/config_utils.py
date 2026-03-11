# utils/config.py

from state import app_state

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

