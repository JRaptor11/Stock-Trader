# === File Paths ===
TRADE_SUMMARY_FILE = "trade_summary.csv"
TRADE_HISTORY_FILE = "trade_history.csv"

# === Trade Rate Limits ===
TRADE_WINDOW = 10  # seconds
TRADE_LIMIT = 5
TRADE_COOLDOWN = 10  # seconds
BUY_ORDER_THROTTLE_SECONDS = 30
MIN_ORDER_AGE_SECONDS = 30
TRADE_RATE_RESPONSE = "cooldown"
MIN_REENTRY_CHANGE_PCT = 0.0004  # Default 0.2%
BUY_CONFIDENCE_THRESHOLD = 0.40
SELL_CONFIDENCE_THRESHOLD = -0.40
CONFIDENCE_CONFLICT_MARGIN = 0.18

# === Fail-Safe Thresholds ===
EQUITY_THRESHOLD = 10000               # Global equity failsafe
EQUITY_FAILSAFE_COOLDOWN = 300         # seconds
MAX_POSITION_LOSS_PERCENT = 0.05       # Per-position max loss
MAX_EQUITY_LOSS = 0.90                 # Global account equity loss
MAX_POSITION_LOSS = 0.95               # Individual position loss
MAX_CONNECTION_ERRORS = 5
CONNECTION_COOLDOWN = 5

# === System Resource Limits ===
MEMORY_ALERT_MB = 500
CPU_ALERT_PERCENT = 80
