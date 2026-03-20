import time
import threading
from threading import RLock, Event
import asyncio
import logging
import contextvars
from collections import deque
from smart_deque import SmartDeque
from types import MappingProxyType

from utils.market_data import MarketDataBuffer

"""
# === Locks for Synchronization ===
stream_lock = threading.Lock()
position_lock = asyncio.Lock()

# === Shared Application State ===

app_state = {
    "email_suppressed": False,  # Flag to stop sending emails if Gmail limit is hit
    "email_suppression_reset": None,  # Date to reset the suppression (midnight UTC)
    "event_loop": None,
    "SYMBOL": [],
    "data_stream": None,
    "has_position": False,
    "last_signal": None,
    "open_trades": {},
    "last_buy_order_time": 0,
    "last_trade_time": None,
    "position_tracker": None,
    "performance_monitor": None,
    "balance_tracker": None,
    "test_run": False,
    "window_size": 20,
    "signal_history": SmartDeque(maxlen=3),
    "trade_timestamps": SmartDeque(maxlen=100),
    "connection_error_count": 0,
    "in_trade_cooldown": False,
    "min_hold_seconds": 1,
    "async_tasks": set()
}

app_state["stream"] = {
    "running": False,
    "loop": None,
    "thread": None,
    "stop_event": threading.Event(),
    "instance": None,
    "heartbeat_running": False,
    "heartbeat_thread": None
}

# Delayed assignment for deques dependent on window_size
app_state["recent_prices"] = SmartDeque(maxlen=app_state["window_size"])
app_state["recent_volumes"] = SmartDeque(maxlen=app_state["window_size"])
"""

# === Shared Application State ===
app_state = {
    "order_state": {
        "entry_locks": {},              # symbol -> timestamp when lock was set
        "entry_lock_timeout_seconds": 90,
    },

    # ─────────────────────────────────────────────
    # 🧠 MAIN CONFIGURATION
    # ─────────────────────────────────────────────
    "main": {
        "symbol": [],
        "async_tasks": set(),    # Tracks all asyncio tasks for shutdown
        "starting_equity": 0.,
        "services": {}
    },

    "secrets": {},
    "paths": {},

    "log_level":{},

    # ─────────────────────────────────────────────
    # 🫀 HEARTBEAT MONITOR
    # ─────────────────────────────────────────────
    "heartbeat": {
        "last_beat": time.time(),
        "instance": None
    },

    # ─────────────────────────────────────────────
    # ⏯️ STREAM & BACKGROUND THREADS
    # ─────────────────────────────────────────────
    "stream": {

        "debug": {
            "status": "init",
            "last_restart": None,
            "last_trade": None
        },
        
        "running": False,
        "loop": None,
        "thread": None,
        "stop_event": None,
        "shutdown_event": threading.Event(),
        "lock": threading.Lock(),
        "allow_multiple_positions": False,
        "last_shutdown_duration": None,
        "instance": None,
        "heartbeat_running": False,
        "heartbeat_thread": None,
        "stopping": False,  

        "connections": {
            "total_attempts": 0,
            "successful": 0,
            "failed": 0,
            "last_success": None,
            "last_failure": None
        },

    },
    
    # ─────────────────────────────────────────────
    # 📦 MARKET DATA BUFFER (per-symbol tick storage)
    # ─────────────────────────────────────────────
    "market_data": {  # NEW
        # Keep this comfortably above VolatilityScorer.lookback so volatility,
        # momentum, and future derived-feature calculations have enough history.
        "buffer": MarketDataBuffer(maxlen_prices=100, maxlen_volumes=100),
    },

    # ─────────────────────────────────────────────
    # 📡 TELEGRAM BOT
    # ─────────────────────────────────────────────
    "telegram": {
        "bot_started": False
    },
    
    # ─────────────────────────────────────────────
    # 📡 CONSTANT CONFIGURATION
    # ─────────────────────────────────────────────
    "config_defaults": {},    # Populated in lifespan()
    "config_overrides": {},   # Populated in lifespan() or via /config
    
    # ─────────────────────────────────────────────
    # 📡 DATA & SYMBOL CONFIGURATION
    # ─────────────────────────────────────────────
    "data_stream": None,  # May duplicate "stream" -> "instance" and be phased out
    "event_loop": None,

    # ─────────────────────────────────────────────
    # 📊 TRADE STATE
    # ─────────────────────────────────────────────
    "open_trades": {},
    "last_signal": None,
    "last_trade_time": None,
    "last_buy_order_time": 0,
    "last_trade_price_by_symbol": {},
    "min_hold_override": None,
    "in_trade_cooldown": False,

    # ─────────────────────────────────────────────
    # 🧠 STRATEGY LOGIC
    # ─────────────────────────────────────────────
    "strategy": {
        "signal_history": SmartDeque(maxlen=3),
        # "recent_prices": SmartDeque(maxlen=25),
        # "recent_volumes": SmartDeque(maxlen=25),
        "recent_prices_by_symbol": {},
        "recent_volumes_by_symbol": {},
        
        "config": {
            "price_window": 200,
            "volume_window": 200,
        },
        "window_size": 20,

        "latest_rsi": None,
        
        "consecutive_losses": 0,
        "cooldown_until": 0,
        
        "min_hold_seconds": 1,
        "min_hold_override": None,  # Set dynamically by AdaptiveHoldStrategy
    
        "volatility_block_log": [],  # Logs blocked trades from volatility filter

        "sells_in_progress": set(),

        "last_exit_reason": "Standard",
        "last_sell": {}
    },

    # ─────────────────────────────────────────────
    # 🧩 SERVICES LOGIC
    # ─────────────────────────────────────────────
    
    "services": {
    
        # 🔄 Position Tracker
        "position_tracker": {
            "running": False,
            "positions": {},
            "lock": threading.Lock(),
            "instance": None
        },
    
        # 📉 Performance Monitor
        "performance_monitor": {
            "max_drawdown": 0.0,
            "lock": threading.Lock(),
            "instance": None
        },
    
        # 📦 Order Executor
        "order_executor": {
            "queue": asyncio.Queue(),
            "lock": threading.Lock(),
            "instance": None
        },
    
        # 🕒 Market Hours Monitor
        "market_monitor": {
            "market_open": False,
            "lock": threading.Lock(),
            "instance": None
        },
    
        # 💵 Account Balance Tracker
        "balance_tracker": {
            "balance": 0.0,
            "equity": 0.0,
            "last_updated": None,
            "lock": threading.Lock(),
            "running": False,
            "instance": None
        }
    },

    # ─────────────────────────────────────────────
    # 🚨 Fail Safe Logic
    # ─────────────────────────────────────────────

    "fail_safes": {
        "email_suppressed": False,
        "email_suppression_reset": None,
        "position_lock": asyncio.Lock(),
        "state": False,
        "liquidation_in_progress": set(),
        "last_trigger_reason": None,
    },

    # ─────────────────────────────────────────────
    # 🧰 Utilities Logic
    # ─────────────────────────────────────────────
    "utils": {

        # Alert Utilities
        "alerts_utils": {
            "email_suppressed": False,
            "email_suppression_reset": None,
            "use_telegram": True
        },
        
        # Trade Utilities
        "trades_utils": {
            "trade_timestamps": deque(maxlen=100),
            "in_trade_cooldown": False,
        }
    },

    # ─────────────────────────────────────────────
    # 🛣️ ROUTE STATE
    # ─────────────────────────────────────────────
    "routes": {

        # Public Routes
        "public_routes": {
            # Reserved for future public-route state
            # (e.g. hit counts, cached status snapshots, etc.)
        },

        # Authentication Routes
        "auth_routes": {
            "token_store": set()
        },

        # Admin Routes
        "admin_routes": {
            # Reserved for future admin-route state
            # (e.g. metrics snapshots, admin audit counters, etc.)
        },

        # Development / Debug Routes
        "dev_routes": {
            "test_run": False
        }
    },

    # ─────────────────────────────────────────────
    # 💰 BALANCE / PERFORMANCE TRACKING
    # ─────────────────────────────────────────────
    "trading_client": None,
    "balance_tracker": None,
    "position_tracker": None,
    "performance_monitor": None,
    "order_executor": None,
    "starting_equity": None,
    "equity": None,

    # ─────────────────────────────────────────────
    # 🛠️ SYSTEM / ENVIRONMENT
    # ─────────────────────────────────────────────
    "connection_error_count": 0
}

app_state_lock = RLock()   # use a re-entrant lock; safe for nested “with” usage
fail_safe_event = Event()  # for instant visibility of fail-safe flips

# === Dynamic Adjustment Functions ===

def update_strategy_window_size(new_size):
    app_state["strategy"]["window_size"] = new_size

    # ✅ NEW: resize per-symbol buffers
    md = app_state.get("market_data", {}).get("buffer")
    if md is not None:
        md.set_maxlen(new_size)

    logging.info(f"✅ Strategy window size updated to {new_size}")

def update_signal_history_size(new_size):
    app_state["strategy"]["signal_history"].set_maxlen(new_size)
    logging.info(f"✅ Strategy signal history size updated to {new_size}")

def update_trade_timestamps_size(new_size):
    app_state["utils"]["trades_utils"]["trade_timestamps"] = deque(maxlen=new_size)
    logging.info(f"✅ Trade timestamp buffer size updated to {new_size}.")

# === Prevent Recursive Re-entry (Context-local Flag) ===
active_handle_trade = contextvars.ContextVar("active_handle_trade", default=False)

def norm_symbol(symbol: str) -> str:
    return (symbol or "").upper().strip()

def ensure_symbol_strategy_state(app_state: dict, symbol: str) -> dict:
    """
    Ensures app_state["strategy"]["symbols"][symbol] exists with subdicts:
      - ind: indicators
      - sig: decision flags
      - tel: telemetry
    """
    symbol = norm_symbol(symbol)
    st = app_state.setdefault("strategy", {})
    sym_map = st.setdefault("symbols", {})
    sym_state = sym_map.setdefault(symbol, {})
    sym_state.setdefault("ind", {})
    sym_state.setdefault("sig", {})
    sym_state.setdefault("tel", {})
    return sym_state