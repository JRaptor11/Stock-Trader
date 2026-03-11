# utils/market_data.py
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

from state import SmartDeque


@dataclass
class Tick:
    t: float
    price: float
    volume: float


class MarketDataBuffer:
    """
    Stores recent tick data per-symbol (timestamps, prices, volumes).

    Strategies should generally consume *prices-only* and *volumes-only* lists.
    Timestamped getters are available for components that need Δt (e.g. VolatilityScorer).
    """

    def __init__(self, maxlen_prices: int = 25, maxlen_volumes: int = 25):
        self._lock = threading.Lock()

        # Internal storage ALWAYS keeps timestamps
        self._prices: Dict[str, SmartDeque] = {}   # SmartDeque of (t, price)
        self._volumes: Dict[str, SmartDeque] = {}  # SmartDeque of (t, volume)

        self._maxlen_prices = int(maxlen_prices)
        self._maxlen_volumes = int(maxlen_volumes)

    def _norm_symbol(self, symbol: str) -> str:
        return (symbol or "").upper().strip()

    def _ensure_symbol(self, symbol: str) -> None:
        if symbol not in self._prices:
            self._prices[symbol] = SmartDeque(maxlen=self._maxlen_prices)
        if symbol not in self._volumes:
            self._volumes[symbol] = SmartDeque(maxlen=self._maxlen_volumes)

    def update_tick(
        self,
        symbol: str,
        price: float,
        volume: float,
        timestamp: float,
    ) -> None:
        symbol = self._norm_symbol(symbol)
        if not symbol:
            return

        with self._lock:
            self._ensure_symbol(symbol)
            self._prices[symbol].append((float(timestamp), float(price)))
            self._volumes[symbol].append((float(timestamp), float(volume)))
            
    # ─────────────────────────────────────────────
    # ✅ Prices-only / volumes-only (preferred for strategies)
    # ─────────────────────────────────────────────
    def get_recent_prices(self, symbol: str, limit: int | None = None) -> List[float]:
        symbol = self._norm_symbol(symbol)
        with self._lock:
            dq = self._prices.get(symbol)
            if not dq:
                return []
            data = [p for (_t, p) in dq]
            return data[-limit:] if (limit and limit > 0) else data

    def get_recent_volumes(self, symbol: str, limit: int | None = None) -> List[float]:
        symbol = self._norm_symbol(symbol)
        with self._lock:
            dq = self._volumes.get(symbol)
            if not dq:
                return []
            data = [v for (_t, v) in dq]
            return data[-limit:] if (limit and limit > 0) else data

    # ─────────────────────────────────────────────
    # ✅ Timestamped getters (for Δt logic like VolatilityScorer)
    # ─────────────────────────────────────────────
    def get_recent_prices_ts(self, symbol: str, limit: int | None = None) -> List[Tuple[float, float]]:
        symbol = self._norm_symbol(symbol)
        with self._lock:
            dq = self._prices.get(symbol)
            data = list(dq) if dq else []
            return data[-limit:] if (limit and limit > 0) else data

    def get_recent_volumes_ts(self, symbol: str, limit: int | None = None) -> List[Tuple[float, float]]:
        symbol = self._norm_symbol(symbol)
        with self._lock:
            dq = self._volumes.get(symbol)
            data = list(dq) if dq else []
            return data[-limit:] if (limit and limit > 0) else data

    def get_last_price(self, symbol: str) -> Optional[float]:
        prices = self.get_recent_prices(symbol)
        return prices[-1] if prices else None

    def set_maxlen(self, prices_maxlen: Optional[int] = None, volumes_maxlen: Optional[int] = None) -> None:
        with self._lock:
            if prices_maxlen is not None:
                self._maxlen_prices = int(prices_maxlen)
                for dq in self._prices.values():
                    dq.set_maxlen(self._maxlen_prices)

            if volumes_maxlen is not None:
                self._maxlen_volumes = int(volumes_maxlen)
                for dq in self._volumes.values():
                    dq.set_maxlen(self._maxlen_volumes)

    def snapshot(self) -> dict:
        with self._lock:
            out = {}
            for sym, dq in self._prices.items():
                tail = list(dq)[-5:]
                out[sym] = {"prices_tail_ts": tail, "len": len(dq)}
            return out