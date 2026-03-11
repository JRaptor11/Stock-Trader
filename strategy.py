import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Number
from typing import Any, List, Optional, Tuple

import numpy as np

from state import (
    app_state,
    app_state_lock,
    ensure_symbol_strategy_state,
    fail_safe_event,
    norm_symbol,
)
from utils.trade_utils import log_trade_decision

# ─────────────────────────────────────────────
# Safe Context Accessor
# ─────────────────────────────────────────────

def ctx(context: dict, key: str, default=None):
    """
    Safe context getter for strategies.

    Prevents KeyError / None crashes when reading values from context.
    """
    try:
        value = context.get(key, default)
        if value is None:
            return default
        return value
    except Exception:
        return default

def ctx_symbol(context: dict, key: str = "symbol") -> str:
    return norm_symbol(ctx(context, key, ""))

def clean_symbol(symbol: str) -> str:
    return norm_symbol(symbol)

def ctx_list(context, key):
    val = ctx(context, key, [])
    if isinstance(val, list):
        return val
    if isinstance(val, deque):
        return list(val)
    if isinstance(val, tuple):
        return list(val)
    return []

def ctx_float(context, key):
    val = context.get(key)
    return float(val) if isinstance(val, (int, float)) else 0.0

# ─────────────────────────────────────────────
# Base accessor
# ─────────────────────────────────────────────
def _md_buffer():
    """Return the shared MarketDataBuffer instance if available."""
    return app_state.get("market_data", {}).get("buffer")


# ─────────────────────────────────────────────
# 1) Prices-only / volumes-only (preferred by most strategies)
# ─────────────────────────────────────────────
def get_recent_prices(symbol: str, limit: Optional[int] = None) -> List[float]:
    """
    Return [price, ...] for this symbol.
    Falls back to legacy global storage only if MarketDataBuffer is unavailable.
    """
    symbol = clean_symbol(symbol)
    if not symbol:
        return []

    md = _md_buffer()
    if md is not None:
        data = list(md.get_recent_prices(symbol) or [])
        return data[-limit:] if (limit and limit > 0) else data

    rp = app_state.get("strategy", {}).get("recent_prices")
    pts = list(rp) if rp is not None else []
    if limit and limit > 0:
        pts = pts[-limit:]

    out: List[float] = []
    for item in pts:
        try:
            a, b = item
            out.append(float(b) if a > 1e9 else float(a))
        except Exception:
            continue
    return out


def get_recent_volumes(symbol: str, limit: Optional[int] = None) -> List[float]:
    """
    Return [volume, ...] for this symbol.
    Falls back to legacy global storage only if MarketDataBuffer is unavailable.
    """
    symbol = clean_symbol(symbol)
    if not symbol:
        return []

    md = _md_buffer()
    if md is not None:
        data = list(md.get_recent_volumes(symbol) or [])
        return data[-limit:] if (limit and limit > 0) else data

    rv = app_state.get("strategy", {}).get("recent_volumes")
    pts = list(rv) if rv is not None else []
    if limit and limit > 0:
        pts = pts[-limit:]

    out: List[float] = []
    for item in pts:
        try:
            a, b = item
            out.append(float(b) if a > 1e9 else float(a))
        except Exception:
            continue
    return out


# ─────────────────────────────────────────────
# 2) Timestamped points (for Δt logic)
# ─────────────────────────────────────────────
def get_recent_price_points(symbol: str, limit: Optional[int] = None) -> List[Tuple[float, float]]:
    """
    Return [(ts, price), ...] for this symbol.
    Falls back to legacy global storage only if MarketDataBuffer is unavailable.
    """
    symbol = clean_symbol(symbol)
    if not symbol:
        return []

    md = _md_buffer()
    if md is not None:
        pts = list(md.get_recent_prices_ts(symbol) or [])
        return pts[-limit:] if (limit and limit > 0) else pts

    rp = app_state.get("strategy", {}).get("recent_prices")
    pts = list(rp) if rp is not None else []
    if limit and limit > 0:
        pts = pts[-limit:]

    out: List[Tuple[float, float]] = []
    for item in pts:
        try:
            a, b = item
            out.append((float(a), float(b)) if a > 1e9 else (float(b), float(a)))
        except Exception:
            continue
    return out


def get_recent_volume_points(symbol: str, limit: Optional[int] = None) -> List[Tuple[float, float]]:
    """
    Return [(ts, volume), ...] for this symbol.
    Falls back to legacy global storage only if MarketDataBuffer is unavailable.
    """
    symbol = clean_symbol(symbol)
    if not symbol:
        return []

    md = _md_buffer()
    if md is not None:
        pts = list(md.get_recent_volumes_ts(symbol) or [])
        return pts[-limit:] if (limit and limit > 0) else pts

    rv = app_state.get("strategy", {}).get("recent_volumes")
    pts = list(rv) if rv is not None else []
    if limit and limit > 0:
        pts = pts[-limit:]

    out: List[Tuple[float, float]] = []
    for item in pts:
        try:
            a, b = item
            out.append((float(a), float(b)) if a > 1e9 else (float(b), float(a)))
        except Exception:
            continue
    return out

# === Base Strategy Instantiation Class ===
class BaseStrategy(ABC):
    @abstractmethod
    def update(self, price: float, context: Optional[dict] = None) -> Any:
        """Return a strategy score, modifier, veto value, or buy/sell score dict."""
        raise NotImplementedError

# === ATR Noise Filter Helper ===
class AtrNoiseFilter:
    def __init__(self, period=14):
        self.period = int(period)
        self._state_by_symbol = {}

    def _sym(self, symbol: str) -> dict:
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "tr_values": deque(maxlen=self.period),
                "prev_close": None,
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def update(self, symbol: str, high: float, low: float, close: float):
        symbol = clean_symbol(symbol)
        if not symbol:
            return None

        st = self._sym(symbol)

        if st["prev_close"] is None:
            st["prev_close"] = close
            return None

        tr = max(
            high - low,
            abs(high - st["prev_close"]),
            abs(low - st["prev_close"])
        )
        st["tr_values"].append(float(tr))
        st["prev_close"] = float(close)

    def get_atr(self, symbol: str):
        symbol = clean_symbol(symbol)
        if not symbol:
            return None

        st = self._sym(symbol)
        if len(st["tr_values"]) < self.period:
            return None
        return sum(st["tr_values"]) / len(st["tr_values"])

    def is_significant_move(self, symbol: str, move_amount: float, multiplier: float = 0.3) -> bool:
        atr = self.get_atr(symbol)
        if atr is None:
            return True
        return abs(move_amount) >= multiplier * atr


@dataclass
class _VolSymState:
    max_seen_vol: float
    min_seen_vol: float
    avg_vol_regime: float
    score_ema: float | None


class VolatilityScorer:
    """
    Measures market volatility using price range and return standard deviation.

    Uses timestamped wrapper:
      - get_recent_price_points(symbol, limit=lookback) -> [(t, price), ...]

    Key behaviors:
    - Computes return-per-second stdev using Δt
    - Maintains per-symbol regime memory + EMA smoothing (no cross-symbol contamination)
    """

    def __init__(
        self,
        lookback=40,
        base_norm=0.00008,
        decay=0.95,
        min_score=0.15,
        vol_sensitivity=1.0,
        fast_bias=0.02,
        stdev_weight=0.7,
        swing_weight=0.3,
        score_smooth=0.2,
    ):
        self.lookback = int(lookback)
        self.base_norm = float(base_norm)
        self.decay = float(decay)
        self.min_score = float(min_score)

        self.vol_sensitivity = float(vol_sensitivity)
        self.fast_bias = float(fast_bias)
        self.stdev_weight = float(stdev_weight)
        self.swing_weight = float(swing_weight)

        self.score_smooth = float(score_smooth)

        # ✅ per-symbol state (regime memory + EMA smoothing)
        self._sym_state: dict[str, _VolSymState] = {}

    # ─────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────
    def _get_state(self, symbol: str) -> _VolSymState:
        symbol = (symbol or "").upper().strip()
        if symbol not in self._sym_state:
            self._sym_state[symbol] = _VolSymState(
                max_seen_vol=self.base_norm,
                min_seen_vol=self.base_norm,
                avg_vol_regime=self.base_norm,
                score_ema=None,
            )
        return self._sym_state[symbol]

    def _split_ts_prices(self, data: List[Any]) -> Tuple[np.ndarray, np.ndarray]:
        """
        Accepts:
          - [(t, price), ...]          (preferred)
          - [(price, t), ...]          (legacy)
          - [price, price, ...]        (fallback)
        Returns: (timestamps, prices) as numpy arrays.
        """
        if not data:
            return np.asarray([], dtype=float), np.asarray([], dtype=float)

        first = data[0]

        # Fallback: prices-only list (shouldn't happen if wrapper is correct,
        # but keep it during migration so it never crashes)
        if not isinstance(first, (tuple, list)) or len(first) != 2:
            prices = np.asarray([float(x) for x in data], dtype=float)
            ts = np.arange(len(prices), dtype=float)  # synthetic Δt=1
            return ts, prices

        a, b = zip(*data)
        a = np.asarray(a, dtype=float)
        b = np.asarray(b, dtype=float)

        # Heuristics: epoch seconds ~ 1e9 and strictly increasing
        a_looks_like_time = (len(a) > 1 and a[0] > 1e9) and np.all(np.diff(a) >= 0)
        b_looks_like_time = (len(b) > 1 and b[0] > 1e9) and np.all(np.diff(b) >= 0)

        if a_looks_like_time and not b_looks_like_time:
            return a, b
        if b_looks_like_time and not a_looks_like_time:
            return b, a

        # Fallback: pick the monotonic series as time
        if len(a) > 1 and np.all(np.diff(a) >= 0) and not np.all(np.diff(b) >= 0):
            return a, b
        if len(b) > 1 and np.all(np.diff(b) >= 0) and not np.all(np.diff(a) >= 0):
            return b, a

        # Last resort: assume (t, price)
        return a, b

    # ─────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────
    def score(self, symbol: str) -> float:
        """
        Compute volatility score for a single symbol (0..1).
        """
        symbol = (symbol or "").upper().strip()
        st = self._get_state(symbol)

        try:
            # ✅ Use the wrapper (handles MarketDataBuffer vs legacy fallback)
            raw_pts = list(get_recent_price_points(symbol, limit=self.lookback))

            if len(raw_pts) < self.lookback:
                logging.debug(f"[VolatilityScorer] {symbol}: Not enough data yet ({len(raw_pts)}/{self.lookback}).")
                return 0.5

            timestamps, prices = self._split_ts_prices(raw_pts[-self.lookback:])
            if len(prices) < 2:
                return 0.0

            reference_price = prices[-1] if prices[-1] != 0 else 1.0

            # Swing component (range normalized by current price)
            swing = (float(np.max(prices)) - float(np.min(prices))) / float(reference_price)

            # Return-per-second component
            time_deltas = np.diff(timestamps)
            valid = time_deltas > 0

            logging.debug(
                f"[VolatilityScorer] {symbol}: time_deltas>0: {int(np.count_nonzero(valid))}/{len(time_deltas)}"
            )

            if not np.any(valid):
                # If timestamps are duplicated/zero, avoid division blowups
                return float(st.score_ema) if st.score_ema is not None else self.min_score

            returns = np.diff(prices) / np.maximum(prices[:-1], 1e-12)
            returns_per_second = returns[valid] / time_deltas[valid]
            stdev = float(np.std(returns_per_second))

            wsum = max(self.stdev_weight + self.swing_weight, 1e-12)
            raw_volatility = float((self.stdev_weight * stdev + self.swing_weight * swing) / wsum)

            # Regime memory (per-symbol)
            st.max_seen_vol = max(st.max_seen_vol * self.decay, raw_volatility)
            st.min_seen_vol = min(st.min_seen_vol / self.decay, raw_volatility)
            st.avg_vol_regime = st.avg_vol_regime * self.decay + raw_volatility * (1 - self.decay)

            # Smooth dynamic normalization
            eps = 1e-12
            delta = (st.avg_vol_regime - raw_volatility) / (st.avg_vol_regime + eps)
            smooth_bump = 0.25 * np.tanh(delta)  # ±0.25 cap
            dynamic_norm = max(self.base_norm, st.avg_vol_regime * (1.0 + smooth_bump))

            # Exponential lift + small bias toward fast weighting
            ratio = raw_volatility / max(dynamic_norm, 1e-12)
            lifted = 1.0 - np.exp(-self.vol_sensitivity * ratio)  # (0,1)
            adjusted = float(np.clip(lifted + self.fast_bias, self.min_score, 1.0))

            # Final EMA smoothing (per-symbol)
            if st.score_ema is None:
                st.score_ema = adjusted
            else:
                alpha = self.score_smooth
                st.score_ema = (1 - alpha) * st.score_ema + alpha * adjusted

            adjusted_score = round(float(st.score_ema), 4)

            logging.debug(
                f"[VolatilityScorer] {symbol}: swing={swing:.4f}, stdev={stdev:.6f}, raw_vol={raw_volatility:.6f}, "
                f"max_seen_vol={st.max_seen_vol:.6f}, avg_vol={st.avg_vol_regime:.6f}, "
                f"min_seen_vol={st.min_seen_vol:.6f}, dynamic_norm={dynamic_norm:.6f}, "
                f"ratio={ratio:.3f}, adjusted_score={adjusted_score:.4f}"
            )

            return adjusted_score

        except Exception as e:
            logging.exception(f"[VolatilityScorer] {symbol}: Error computing score: {e}")
            return float(st.score_ema) if st.score_ema is not None else self.min_score

class ConfidenceModel:
    def __init__(self, app_state, strategy_metadata):
        self.app_state = app_state
        self.strategy_metadata = strategy_metadata
        self._buy_conf_ema = {}
        self._sell_conf_ema = {}

        # Base smoothing fallback
        self.conf_smooth_alpha = 0.25

        # Confidence scaling controls
        self.confidence_scale = 2.35          # was 3.0; reduce over-amplification
        self.soft_brake_cap = 0.22            # slightly softer than before
        self.modifier_floor = 0.60            # keep filters meaningful without crushing confidence
        self.min_active_weight = 1e-9

        # New: prevent one speed bucket from dominating too hard
        self.max_speed_contribution = 0.72

    @staticmethod
    def _is_num(x):
        return isinstance(x, Number)

    @staticmethod
    def _clamp11(x: float) -> float:
        return max(-1.0, min(1.0, x))

    def _split_scalar(self, s: float) -> tuple[float, float]:
        """
        Legacy mapping for scalar scores in [-1,1]:
          positive -> buy channel
          negative -> sell channel
        """
        s = float(s)
        buy_part = max(0.0, s)
        sell_part = min(0.0, s)
        return buy_part, sell_part

    @staticmethod
    def _signed_scale(x: float, scale: float) -> float:
        """
        Preserve sign while lifting confidence magnitude so strong active signals
        can actually reach useful thresholds.
        """
        if x == 0.0:
            return 0.0
        return max(-1.0, min(1.0, x * scale))

    @staticmethod
    def _resolve_alpha(volatility_score: float) -> float:
        """
        Slightly more responsive EMA in high volatility.
        """
        if volatility_score >= 0.80:
            return 0.45
        if volatility_score >= 0.55:
            return 0.32
        return 0.25

    @staticmethod
    def _compute_modifier(modifiers: list[float], floor: float) -> float:
        """
        Softer than multiplicative stacking.
        Uses average modifier with a floor so multiple filters do not crush confidence.
        """
        if not modifiers:
            return 1.0

        clean = [float(m) for m in modifiers if isinstance(m, (int, float))]
        if not clean:
            return 1.0

        avg = sum(clean) / len(clean)
        return max(floor, min(1.0, avg))

    @staticmethod
    def _cap_signed_magnitude(x: float, cap: float) -> float:
        """
        Preserve sign while capping magnitude.
        Helps prevent a single overweighted bucket from dominating the blend.
        """
        if x > cap:
            return cap
        if x < -cap:
            return -cap
        return x

    def _extract_signal_parts(self, raw_score):
        """
        Returns (buy_part, sell_part) from either:
          - dict: {"buy": x, "sell": y}
          - scalar in [-1,1]
        """
        if isinstance(raw_score, dict):
            b = raw_score.get("buy", 0.0)
            s = raw_score.get("sell", 0.0)

            if not self._is_num(b) and not self._is_num(s):
                return None, None

            buy_part = float(b) if self._is_num(b) else 0.0
            sell_part = float(s) if self._is_num(s) else 0.0
            return buy_part, sell_part

        if self._is_num(raw_score):
            return self._split_scalar(raw_score)

        return None, None

    def _accumulate_signal(
        self,
        buy_part: float,
        sell_part: float,
        weight: float,
        speed: str,
        accum: dict,
    ) -> None:
        """
        Accumulate weighted contributions by speed bucket.
        Only count weight when that side is actually active, so strong active signals
        are not diluted by inactive strategy slots.
        """
        if speed == "fast":
            if buy_part > 0.0:
                accum["buy_fast_sum"] += buy_part * weight
                accum["buy_fast_w"] += abs(weight)
            if sell_part < 0.0:
                accum["sell_fast_sum"] += sell_part * weight
                accum["sell_fast_w"] += abs(weight)

        elif speed == "lagging":
            if buy_part > 0.0:
                accum["buy_lag_sum"] += buy_part * weight
                accum["buy_lag_w"] += abs(weight)
            if sell_part < 0.0:
                accum["sell_lag_sum"] += sell_part * weight
                accum["sell_lag_w"] += abs(weight)

    def get_confidence(self, signal_results: dict, symbol: str, **kwargs) -> tuple[float, float]:
        context = kwargs.get("context") or {}
        symbol = clean_symbol(symbol)

        accum = {
            "buy_fast_sum": 0.0,
            "buy_fast_w": 0.0,
            "sell_fast_sum": 0.0,
            "sell_fast_w": 0.0,
            "buy_lag_sum": 0.0,
            "buy_lag_w": 0.0,
            "sell_lag_sum": 0.0,
            "sell_lag_w": 0.0,
        }

        buy_modifiers = []
        sell_modifiers = []

        buy_soft_hits = 0.0
        buy_soft_total = 0.0
        sell_soft_hits = 0.0
        sell_soft_total = 0.0

        for name, raw_score in signal_results.items():
            meta = self.strategy_metadata.get(name, {})
            wtype = meta.get("weight_type")
            speed = meta.get("speed")
            weight = float(meta.get("weight", 1.0))
            direction = meta.get("direction", "both")
            veto_strength = meta.get("veto_strength")

            if wtype == "signal":
                buy_part, sell_part = self._extract_signal_parts(raw_score)
                if buy_part is None and sell_part is None:
                    continue

                self._accumulate_signal(
                    buy_part=buy_part,
                    sell_part=sell_part,
                    weight=weight,
                    speed=speed,
                    accum=accum,
                )

            elif wtype == "confidence_modifier":
                if not self._is_num(raw_score):
                    continue

                # numeric filters expected around [0,1]
                raw_mod = float(raw_score)
                local_floor = float(meta.get("floor", self.modifier_floor))
                modifier = max(local_floor, min(1.0, raw_mod))

                if direction in ("both", "buy"):
                    buy_modifiers.append(modifier)
                if direction in ("both", "sell"):
                    sell_modifiers.append(modifier)

                if veto_strength == "soft":
                    if direction in ("both", "buy"):
                        buy_soft_hits += 1.0 - modifier
                        buy_soft_total += 1.0
                    if direction in ("both", "sell"):
                        sell_soft_hits += 1.0 - modifier
                        sell_soft_total += 1.0

        # --- Active-signal averages only ---
        buy_fast_avg = (
            accum["buy_fast_sum"] / max(accum["buy_fast_w"], self.min_active_weight)
            if accum["buy_fast_w"] > 0.0 else 0.0
        )
        buy_lag_avg = (
            accum["buy_lag_sum"] / max(accum["buy_lag_w"], self.min_active_weight)
            if accum["buy_lag_w"] > 0.0 else 0.0
        )
        sell_fast_avg = (
            accum["sell_fast_sum"] / max(accum["sell_fast_w"], self.min_active_weight)
            if accum["sell_fast_w"] > 0.0 else 0.0
        )
        sell_lag_avg = (
            accum["sell_lag_sum"] / max(accum["sell_lag_w"], self.min_active_weight)
            if accum["sell_lag_w"] > 0.0 else 0.0
        )

        # New: cap per-bucket magnitude so one heavily weighted strategy
        # cannot overwhelm the final blend too easily.
        buy_fast_avg = self._cap_signed_magnitude(buy_fast_avg, self.max_speed_contribution)
        buy_lag_avg = self._cap_signed_magnitude(buy_lag_avg, self.max_speed_contribution)
        sell_fast_avg = self._cap_signed_magnitude(sell_fast_avg, self.max_speed_contribution)
        sell_lag_avg = self._cap_signed_magnitude(sell_lag_avg, self.max_speed_contribution)

        # --- Volatility lookup ---
        volatility_score = ctx_float(context, "volatility_score")
        if volatility_score <= 0.0:
            vol_scorer = self.app_state["strategy"].get("volatility_scorer")
            volatility_score = float(vol_scorer.score(symbol)) if (vol_scorer and symbol) else 0.5

        volatility_score = max(0.0, min(1.0, float(volatility_score)))
        self.app_state["strategy"].setdefault("volatility_score_by_symbol", {})[symbol] = volatility_score

        # --- Fast/Lag blend ---
        fast_w = volatility_score
        lag_w = 1.0 - volatility_score

        raw_buy_blended = (buy_fast_avg * fast_w) + (buy_lag_avg * lag_w)
        raw_sell_blended = (sell_fast_avg * fast_w) + (sell_lag_avg * lag_w)

        # --- Modifier aggregation (soft average, not multiplicative stacking) ---
        buy_modifier = self._compute_modifier(buy_modifiers, self.modifier_floor)
        sell_modifier = self._compute_modifier(sell_modifiers, self.modifier_floor)

        mod_buy = raw_buy_blended * buy_modifier
        mod_sell = raw_sell_blended * sell_modifier

        # --- Soft brake penalties ---
        if buy_soft_total > 0.0:
            buy_soft_penalty = min(self.soft_brake_cap, buy_soft_hits / buy_soft_total)
            mod_buy *= (1.0 - buy_soft_penalty)
        else:
            buy_soft_penalty = 0.0

        if sell_soft_total > 0.0:
            sell_soft_penalty = min(self.soft_brake_cap, sell_soft_hits / sell_soft_total)
            mod_sell *= (1.0 - sell_soft_penalty)
        else:
            sell_soft_penalty = 0.0

        # --- Confidence lift so thresholds like 0.40 remain meaningful ---
        scaled_buy = self._signed_scale(mod_buy, self.confidence_scale)
        scaled_sell = self._signed_scale(mod_sell, self.confidence_scale)

        # --- EMA smoothing ---
        prev_buy = self._buy_conf_ema.get(symbol)
        prev_sell = self._sell_conf_ema.get(symbol)
        alpha = self._resolve_alpha(volatility_score)

        self._buy_conf_ema[symbol] = (
            scaled_buy if prev_buy is None
            else ((1.0 - alpha) * prev_buy + alpha * scaled_buy)
        )
        self._sell_conf_ema[symbol] = (
            scaled_sell if prev_sell is None
            else ((1.0 - alpha) * prev_sell + alpha * scaled_sell)
        )

        buy_conf = round(self._clamp11(self._buy_conf_ema[symbol]), 4)
        sell_conf = round(self._clamp11(self._sell_conf_ema[symbol]), 4)

        logging.debug(
            f"[ConfidenceModel] "
            f"BUY fast={buy_fast_avg:.2f}/{accum['buy_fast_w']:.2f} "
            f"lag={buy_lag_avg:.2f}/{accum['buy_lag_w']:.2f} | "
            f"SELL fast={sell_fast_avg:.2f}/{accum['sell_fast_w']:.2f} "
            f"lag={sell_lag_avg:.2f}/{accum['sell_lag_w']:.2f} | "
            f"Vol[{symbol}]={volatility_score:.2f} fast_w={fast_w:.2f} lag_w={lag_w:.2f} | "
            f"raw_buy={raw_buy_blended:.4f} raw_sell={raw_sell_blended:.4f} | "
            f"buy_mod={buy_modifier:.2f} sell_mod={sell_modifier:.2f} | "
            f"buy_soft_pen={buy_soft_penalty:.2f} sell_soft_pen={sell_soft_penalty:.2f} | "
            f"scaled_buy={scaled_buy:.4f} scaled_sell={scaled_sell:.4f} | "
            f"ema_buy={buy_conf:.4f} ema_sell={sell_conf:.4f}"
        )

        return buy_conf, sell_conf


# === Volatility Reversal Brake Class ===
class VolatilityReversalBrake(BaseStrategy):
    def __init__(self, window=5, volatility_threshold=0.002, signal_flip_tolerance=2):
        self.window = window
        self.volatility_threshold = volatility_threshold
        self.signal_flip_tolerance = signal_flip_tolerance
        self._state_by_symbol = {}

    def _sym(self, symbol: str) -> dict:
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "last_signal": None,
                "flip_count": 0,
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        recent_prices = ctx_list(context, "recent_prices")
        sig = ctx(context, "sig", {})
        tel = ctx(context, "tel", {})

        if not symbol:
            return 1.0

        st = self._sym(symbol)

        if len(recent_prices) < self.window:
            return 1.0

        prices = recent_prices[-self.window:]
        price_range = max(prices) - min(prices)
        normalized_vol = price_range / prices[0] if prices[0] else 1.0
        tel["vrb_norm_vol"] = float(normalized_vol)

        if normalized_vol > self.volatility_threshold:
            current_signal = sig.get("last_raw_signal")

            if current_signal != st["last_signal"] and current_signal in ("buy", "sell"):
                st["flip_count"] += 1
            else:
                st["flip_count"] = 0

            st["last_signal"] = current_signal
            tel["vrb_flip_count"] = int(st["flip_count"])

            if st["flip_count"] >= self.signal_flip_tolerance:
                return 0.0
        else:
            st["flip_count"] = 0

        return 1.0


# === Basic Volatility Filter Class ===
class BasicVolatilityFilter(BaseStrategy):
    def __init__(self, max_pct_swing=0.015, window=5):
        self.max_pct_swing = max_pct_swing
        self.window = window

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        recent_prices = ctx_list(context, "recent_prices")
        tel = ctx(context, "tel", {})

        if len(recent_prices) < self.window:
            return 1.0

        window_prices = recent_prices[-self.window:]
        max_price = max(window_prices)
        min_price = min(window_prices)
        swing = (max_price - min_price) / price if price else 1.0

        tel["vol_filter_swing"] = float(swing)

        if swing > self.max_pct_swing:
            return 0.0

        return 1.0


class SmartTrendFilterStrategy(BaseStrategy):
    def __init__(self, trend_window=100, threshold_pct=0.005, rsi_trigger=25, volume_boost_ratio=1.5):
        self.mult = 2 / (trend_window + 1)
        self.threshold_pct = threshold_pct
        self.rsi_trigger = rsi_trigger
        self.volume_boost_ratio = volume_boost_ratio
        self._trend_ema_by_symbol = {}

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        ind = ctx(context, "ind", {})
        tel = ctx(context, "tel", {})
        recent_volumes = ctx_list(context, "recent_volumes")

        if not symbol:
            return 1.0

        trend_ema = self._trend_ema_by_symbol.get(symbol)
        if trend_ema is None:
            trend_ema = float(price)
        else:
            trend_ema = (float(price) - trend_ema) * self.mult + trend_ema
        self._trend_ema_by_symbol[symbol] = trend_ema

        tel["trend_ema"] = float(trend_ema)

        atr_filter = app_state["strategy"].get("atr_filter")
        delta = float(price) - trend_ema
        if atr_filter and not atr_filter.is_significant_move(symbol, delta):
            return 1.0

        if float(price) >= trend_ema * (1 - self.threshold_pct):
            return 1.0

        rsi = ind.get("latest_rsi")
        if rsi is not None and rsi < self.rsi_trigger:
            return 1.0

        if len(recent_volumes) >= 10:
            avg_volume = sum(recent_volumes[-10:]) / 10
            current_volume = recent_volumes[-1]
            if current_volume > avg_volume * self.volume_boost_ratio:
                return 1.0

        return 0.0


# === Volume Spike Strategy Class ===
class VolumeSpikeStrategy(BaseStrategy):
    def __init__(self, window=10, min_relative_volume=0.5):
        self.window = window
        self.min_relative_volume = min_relative_volume

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        vols = ctx_list(context, "recent_volumes")
        tel = ctx(context, "tel", {})

        if len(vols) < self.window:
            tel["volume_confidence_modifier"] = 1.0
            return 1.0

        current_volume = vols[-1]
        avg_volume = sum(vols[-self.window:]) / self.window
        relative_vol = current_volume / avg_volume if avg_volume > 0 else 0.0

        modifier = min(relative_vol / self.min_relative_volume, 1.0)
        tel["volume_confidence_modifier"] = float(modifier)
        tel["rel_volume"] = float(relative_vol)

        return 0.0 if relative_vol < self.min_relative_volume else 1.0


class VolumeBreakoutStrategy(BaseStrategy):
    """
    Dual-channel volume breakout / breakdown detector.

    Returns:
        {"buy": float in [0,1], "sell": float in [-1,0]}

    Design:
    - Uses relative volume vs. trailing average and a price breakout/breakdown check.
    - Graded intensity: stronger relative volume and bigger price escape from the prior range → higher magnitude.
    - Pre-threshold hint: small readiness signal when very close to threshold to avoid binary behavior.
    - ATR gate (optional): if move isn’t “significant”, require stronger relative volume to pass.
    - Light state-aware shaping:
        * Flat → nudge buys on up-breakouts, lightly damp sells.
        * Holding → nudge sells on down-breakdowns, lightly damp buys.
      (This strategy can work state-agnostic, but mild nudges align it with position context.)
    """
    def __init__(self, window=10, spike_multiplier=1.35, min_price_move=0.0, prehint_fraction=0.35):
        self.window = int(window)
        self.spike_multiplier = float(spike_multiplier)
        self.min_price_move = float(min_price_move)
        self.prehint_fraction = float(prehint_fraction)
        self._recent_net_by_symbol = {}

    def update(self, price: float, context: Optional[dict] = None) -> dict:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        has_position = bool(ctx(context, "has_position", False))
        recent_prices = ctx_list(context, "recent_prices")
        recent_volumes = ctx_list(context, "recent_volumes")
        tel = ctx(context, "tel", {})
        volatility_score = ctx_float(context, "volatility_score")

        if not symbol:
            return {"buy": 0.0, "sell": 0.0}

        if len(recent_prices) < self.window or len(recent_volumes) < self.window:
            return {"buy": 0.0, "sell": 0.0}

        prices = recent_prices[-self.window:]
        prev_slice = prices[:-1]
        curr = float(f"{prices[-1]:.2f}")

        volumes = recent_volumes[-self.window:]
        current_volume = float(volumes[-1])
        avg_slice = volumes[:-1]
        if not avg_slice:
            return {"buy": 0.0, "sell": 0.0}
        avg_volume = sum(avg_slice) / len(avg_slice) if sum(avg_slice) > 0 else 0.0
        relative_vol = (current_volume / avg_volume) if avg_volume > 0 else 0.0

        atr_filter = app_state["strategy"].get("atr_filter")
        price_change = curr - prev_slice[-1]
        if atr_filter and not atr_filter.is_significant_move(symbol, price_change):
            if relative_vol < (self.spike_multiplier * 1.1):
                return {"buy": 0.0, "sell": 0.0}

        prev_high = max(prev_slice)
        prev_low = min(prev_slice)

        bullish_breakout = curr > prev_high
        bearish_breakdown = curr < prev_low

        thr = max(1e-9, self.spike_multiplier)
        over = max(0.0, relative_vol - thr) / thr
        base = max(0.0, min(over, 1.0))

        hint_band = self.prehint_fraction * thr
        hint = 0.0
        if relative_vol < thr and hint_band > 0:
            hint = min(0.25, (relative_vol / max(hint_band, 1e-9)) * 0.25)

        up_escape = (
            (curr - prev_high) / (prev_high + 1e-9)
            if bullish_breakout
            else max(0.0, (curr - prev_high) / (0.001 * prev_high + 1e-9))
        )
        down_escape = (
            (prev_low - curr) / (prev_low + 1e-9)
            if bearish_breakdown
            else max(0.0, (prev_low - curr) / (0.001 * prev_low + 1e-9))
        )

        up_mag = min(1.0, up_escape / 0.004)
        down_mag = min(1.0, down_escape / 0.004)

        buy_raw = 0.0
        sell_raw_abs = 0.0

        if bullish_breakout:
            buy_raw = min(1.0, base * (0.6 + 0.4 * up_mag))
            if base < 0.2 and up_mag < 0.2:
                sell_raw_abs = max(sell_raw_abs, 0.05)
        elif bearish_breakdown:
            sell_raw_abs = min(1.0, base * (0.6 + 0.4 * down_mag))
            if base < 0.2 and down_mag < 0.2:
                buy_raw = max(buy_raw, 0.05)
        else:
            near_high = (curr - prev_high) / max(1e-9, prev_high)
            near_low = (prev_low - curr) / max(1e-9, prev_low)
            if near_high > -0.0005:
                buy_raw = max(buy_raw, hint * max(0.0, 1.0 + near_high * 200.0))
            if near_low > -0.0005:
                sell_raw_abs = max(sell_raw_abs, hint * max(0.0, 1.0 + near_low * 200.0))

        buy_raw = max(0.0, min(1.0, buy_raw))
        sell_raw_abs = max(0.0, min(1.0, sell_raw_abs))
        sell_raw = -sell_raw_abs

        if volatility_score > 0.8:
            buy_raw *= 0.92
            sell_raw *= 0.92

        if has_position:
            buy_raw *= 0.94
            sell_raw *= 1.06
            if bullish_breakout:
                sell_raw *= 0.95
        else:
            buy_raw *= 1.06
            sell_raw *= 0.94
            if bearish_breakdown:
                sell_raw *= 1.04

        recent_net = self._recent_net_by_symbol.setdefault(symbol, deque(maxlen=6))

        def boost_net(score: float) -> float:
            matches = 0
            for past in reversed(recent_net):
                if score * past > 0:
                    matches += 1
                else:
                    break
            boosted = score * (1.0 + 0.05 * matches)
            return max(min(boosted, 1.0), -1.0)

        net_scalar = buy_raw - (-sell_raw)
        boosted_net = boost_net(net_scalar) if len(recent_net) else net_scalar
        recent_net.append(boosted_net)

        buy_prop = max(1e-6, buy_raw)
        sell_prop = max(1e-6, -sell_raw)
        total_prop = buy_prop + sell_prop
        buy_mix = buy_prop / total_prop
        sell_mix = sell_prop / total_prop

        total_intensity = min(1.0, buy_prop + sell_prop)
        target_buy = max(0.0, (boosted_net + total_intensity) / 2.0)
        target_sell_abs = max(0.0, total_intensity - target_buy)

        final_buy = 0.6 * target_buy + 0.4 * (total_intensity * buy_mix)
        final_sell_abs = 0.6 * target_sell_abs + 0.4 * (total_intensity * sell_mix)

        final_buy = float(max(0.0, min(1.0, round(final_buy, 4))))
        final_sell = float(-max(0.0, min(1.0, round(final_sell_abs, 4))))

        tel["vb_rel_vol"] = round(relative_vol, 3)
        tel["vb_base"] = round(base, 3)
        tel["vb_hint"] = round(hint, 3)
        tel["vb_bull_breakout"] = bullish_breakout
        tel["vb_bear_breakdown"] = bearish_breakdown
        tel["vb_buy"] = final_buy
        tel["vb_sell"] = final_sell

        return {"buy": final_buy, "sell": final_sell}


class PriceSurgeStrategy(BaseStrategy):
    """
    Dual-channel price surge detector over a short window.
    - Always returns both channels:
        {"buy": [0,1], "sell": [-1,0]}
    - Core idea: measure % change from the oldest price in a short window.
    - Produces graded intensity:
        * below threshold: small "hint" (pre-threshold ramp)
        * above threshold: full surge magnitude in [0,1]
    - Light state-aware shaping (optional):
        * Flat: nudge buys on up-surges, damp sells slightly
        * Holding: nudge sells on down-surges, damp buys slightly
    - Volatility softening to reduce whipsaw in high-vol regimes.
    """
    def __init__(self, window=4, threshold_pct=0.6, prehint_fraction=0.35):
        self.window = int(window)
        self.threshold_pct = float(threshold_pct)
        self.prehint_fraction = float(prehint_fraction)
        self.recent_net_by_symbol = {}

    def _boost_net(self, symbol: str, score: float) -> float:
        recent_net = self.recent_net_by_symbol.setdefault(symbol, deque(maxlen=6))
        matches = 0
        for past in reversed(recent_net):
            if score * past > 0:
                matches += 1
            else:
                break
        boosted = score * (1.0 + 0.05 * matches)
        return max(min(boosted, 1.0), -1.0)

    def update(self, price: float, context: Optional[dict] = None) -> dict:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        has_position = bool(ctx(context, "has_position", False))
        recent_prices = ctx_list(context, "recent_prices")
        tel = ctx(context, "tel", {})
        volatility_score = ctx_float(context, "volatility_score")

        if not symbol:
            return {"buy": 0.0, "sell": 0.0}

        if len(recent_prices) < self.window:
            return {"buy": 0.0, "sell": 0.0}

        price = float(f"{price:.2f}")
        oldest = recent_prices[-self.window]
        if oldest == 0:
            return {"buy": 0.0, "sell": 0.0}

        change_pct = ((price - oldest) / oldest) * 100.0
        thr = max(1e-9, self.threshold_pct)

        over = max(0.0, abs(change_pct) - thr) / thr
        base = max(0.0, min(over, 1.0))

        hint_band = self.prehint_fraction * thr
        hint = 0.0
        if abs(change_pct) < thr and hint_band > 0:
            hint = max(0.0, (abs(change_pct) / hint_band)) * 0.25
            hint = min(hint, 0.25)

        if change_pct >= 0:
            buy_raw = base + hint * (abs(change_pct) / max(thr, 1e-9))
            sell_raw_abs = hint * (1.0 - min(1.0, change_pct / thr)) * 0.5
        else:
            sell_raw_abs = base + hint * (abs(change_pct) / max(thr, 1e-9))
            buy_raw = hint * (1.0 - min(1.0, abs(change_pct) / thr)) * 0.5

        buy_raw = max(0.0, min(1.0, buy_raw))
        sell_raw_abs = max(0.0, min(1.0, sell_raw_abs))
        sell_raw = -sell_raw_abs

        if volatility_score > 0.8:
            buy_raw *= 0.92
            sell_raw *= 0.92

        if has_position:
            buy_raw *= 0.94
            sell_raw *= 1.06
            if change_pct > 0:
                sell_raw *= 0.95
        else:
            buy_raw *= 1.06
            sell_raw *= 0.94
            if change_pct < 0:
                sell_raw *= 1.04

        recent_net = self.recent_net_by_symbol.setdefault(symbol, deque(maxlen=6))
        net_scalar = buy_raw - (-sell_raw)
        boosted_net = self._boost_net(symbol, net_scalar) if len(recent_net) else net_scalar
        recent_net.append(boosted_net)

        buy_prop = max(1e-6, buy_raw)
        sell_prop = max(1e-6, -sell_raw)
        total_prop = buy_prop + sell_prop
        buy_mix = buy_prop / total_prop
        sell_mix = sell_prop / total_prop

        total_intensity = min(1.0, buy_prop + sell_prop)
        target_buy = max(0.0, (boosted_net + total_intensity) / 2.0)
        target_sell_abs = max(0.0, total_intensity - target_buy)

        final_buy = 0.6 * target_buy + 0.4 * (total_intensity * buy_mix)
        final_sell_abs = 0.6 * target_sell_abs + 0.4 * (total_intensity * sell_mix)

        final_buy = float(max(0.0, min(1.0, round(final_buy, 4))))
        final_sell = float(-max(0.0, min(1.0, round(final_sell_abs, 4))))

        tel["price_surge_change_pct"] = round(change_pct, 4)
        tel["price_surge_base"] = round(base, 4)
        tel["price_surge_hint"] = round(hint, 4)
        tel["price_surge_buy"] = final_buy
        tel["price_surge_sell"] = final_sell

        return {"buy": final_buy, "sell": final_sell}
        

# === Max Uptick Hold Strategy ===
class MaxUptickHoldStrategy(BaseStrategy):
    def __init__(self, max_hold_seconds=15, min_profit=0.001, max_loss=-0.005):
        self.max_hold_seconds = max_hold_seconds
        self.min_profit = min_profit
        self.max_loss = max_loss

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        trade = ctx(context, "trade_info", None)
        sig = ctx(context, "sig", {})
        tel = ctx(context, "tel", {})

        if not symbol or not trade:
            sig["quick_exit_recommended"] = False
            tel["quick_exit_recommended"] = False
            return 1.0

        buy_price = trade.get("buy_price")
        buy_time = trade.get("buy_time")

        if not buy_price or not buy_time:
            sig["quick_exit_recommended"] = False
            tel["quick_exit_recommended"] = False
            return 1.0

        held = (datetime.now(timezone.utc) - buy_time).total_seconds()
        change = (price - buy_price) / buy_price if buy_price else 0.0

        tel["quick_exit_hold_seconds"] = float(held)
        tel["quick_exit_change"] = float(change)

        if held < self.max_hold_seconds:
            if change <= self.max_loss:
                sig["last_exit_reason"] = "QuickExit"
                sig["quick_exit_recommended"] = True
                tel["quick_exit_recommended"] = True
                return 0.0

        sig["quick_exit_recommended"] = False
        tel["quick_exit_recommended"] = False
        
        return 1.0


# === Momentum Brake Strategy ===
class MomentumBrakeStrategy(BaseStrategy):
    def __init__(self, window=5, max_rate=0.03, soft_block_rate=0.005, rsi_relax_threshold=25):
        self.window = window
        self.max_rate = max_rate
        self.soft_block_rate = soft_block_rate
        self.rsi_relax_threshold = rsi_relax_threshold

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        recent_prices = ctx_list(context, "recent_prices")
        ind = ctx(context, "ind", {})
        tel = ctx(context, "tel", {})

        if len(recent_prices) < self.window:
            return 1.0

        prices = recent_prices[-self.window:]
        rate = (prices[-1] - prices[0]) / prices[0] if prices[0] else 0.0
        rsi = ind.get("latest_rsi")

        tel["momentum_rate"] = float(rate)

        if rate < -self.max_rate:
            if isinstance(rsi, (int, float)) and rsi < self.rsi_relax_threshold:
                return 1.0
            return 0.0

        if rate < -self.soft_block_rate:
            return 0.0

        return 1.0


class VolumeMomentumFilterStrategy(BaseStrategy):
    def __init__(self, rsi_min=40, require_ema_momentum=False, volume_window=5, volume_multiplier=1.1):
        self.base_rsi_min = rsi_min
        self.require_ema_momentum = require_ema_momentum
        self.volume_window = volume_window
        self.volume_multiplier = volume_multiplier

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        ind = ctx(context, "ind", {})
        sig = ctx(context, "sig", {})
        tel = ctx(context, "tel", {})
        recent_volumes = ctx_list(context, "recent_volumes")
        volatility_score = ctx_float(context, "volatility_score")

        if not symbol:
            return 1.0

        has_position = bool(ctx(context, "has_position", False))
        if has_position:
            return 1.0

        rsi = ind.get("latest_rsi")
        rsi_up = ind.get("rsi_momentum_up")
        ema_up = ind.get("ema_momentum_up")
        overall_trend = ind.get("overall_trend", "unknown")
        market_open_time = app_state.get("market_open_time")

        if overall_trend == "up":
            return 1.0

        early_rsi_buffer = 0
        if market_open_time:
            if isinstance(market_open_time, float):
                market_open_time = datetime.fromtimestamp(market_open_time, tz=timezone.utc)
            time_since_open = (datetime.now(timezone.utc).replace(tzinfo=timezone.utc) - market_open_time).total_seconds()
            if time_since_open < 600:
                early_rsi_buffer = 5

        if volatility_score > 0.7:
            adjusted_rsi_min = self.base_rsi_min - 5
        elif volatility_score < 0.3:
            adjusted_rsi_min = self.base_rsi_min + 5
        else:
            adjusted_rsi_min = self.base_rsi_min

        adjusted_rsi_min += early_rsi_buffer
        tel["vmf_adjusted_rsi_min"] = float(adjusted_rsi_min)

        if self.require_ema_momentum and not (rsi_up and ema_up):
            return 0.0

        if rsi is None:
            return 1.0

        if rsi < adjusted_rsi_min:
            return 0.0

        if len(recent_volumes) < self.volume_window:
            return 1.0

        avg_volume = sum(recent_volumes[-self.volume_window:]) / self.volume_window
        current_volume = recent_volumes[-1]
        tel["vmf_avg_volume"] = float(avg_volume)
        tel["vmf_current_volume"] = float(current_volume)

        if current_volume < avg_volume * self.volume_multiplier:
            return 0.0

        return 1.0
        

# === Micro Range Rejection Strategy ===
class MicroRangeRejectionStrategy(BaseStrategy):
    def __init__(self, window=5, threshold=0.002):
        self.window = int(window)
        self.threshold = float(threshold)
        self._state_by_symbol = {}

    def _sym(self, symbol: str) -> dict:
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "recent_prices": deque(maxlen=self.window),
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        if not symbol:
            return 1.0

        st = self._sym(symbol)
        recent_prices = st["recent_prices"]

        price = float(f"{price:.2f}")
        recent_prices.append(price)

        if len(recent_prices) < self.window:
            return 1.0

        high = max(recent_prices)
        low = min(recent_prices)
        price_range = (high - low) / low if low else 0.0

        logging.debug(f"[MicroRangeRejection] {symbol} Range: {price_range:.4%} | High: {high}, Low: {low}")

        if price_range < self.threshold:
            logging.info(f"[MicroRangeRejection] 🛑 Vetoed trade for {symbol} — micro-range detected.")
            return 0.0

        return 1.0

class OverboughtSoftBrakeStrategy(BaseStrategy):
    def __init__(self, rsi_threshold=65, rsi_upper=70, min_macd_slope=0.0003, ema_flat_threshold=0.0003):
        self.rsi_threshold = rsi_threshold
        self.rsi_upper = rsi_upper
        self.min_macd_slope = min_macd_slope
        self.ema_flat_threshold = ema_flat_threshold

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        ind = ctx(context, "ind", {})

        rsi = ind.get("latest_rsi")
        macd_up = ind.get("macd_momentum_up", False)
        ema_slope = ind.get("ema_slope", 0.0)

        if rsi is None:
            return 1.0

        if self.rsi_threshold <= rsi <= self.rsi_upper:
            if not macd_up and abs(ema_slope) < self.ema_flat_threshold:
                return 0.0
        return 1.0


class TopRejectionStrategy(BaseStrategy):
    def __init__(self, rsi_cutoff=70, ema_flat_threshold=0.0002):
        self.rsi_cutoff = rsi_cutoff
        self.ema_flat_threshold = ema_flat_threshold

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        ind = ctx(context, "ind", {})

        rsi = ind.get("latest_rsi")
        ema_up = ind.get("ema_momentum_up", False)
        ema_slope = ind.get("ema_slope", 0.0)

        if rsi is None:
            return 1.0

        if rsi > self.rsi_cutoff and not ema_up and abs(ema_slope) < self.ema_flat_threshold:
            return 0.0

        return 1.0


class DropFromPeakSmartStrategy(BaseStrategy):
    def __init__(self, min_gain=0.0025, drop_from_peak=0.0010, reentry_drop=0.0013, rsi_threshold=50):
        self.min_gain = float(min_gain)
        self.drop_from_peak = float(drop_from_peak)
        self.reentry_drop = float(reentry_drop)
        self.rsi_threshold = rsi_threshold
        self.history_boost = 0.06
        self._state_by_symbol = {}

    def _sym(self, symbol: str) -> dict:
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "recent_net": deque(maxlen=8),
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def _boost_net(self, symbol: str, score: float) -> float:
        st = self._sym(symbol)
        recent_net = st["recent_net"]

        matches = 0
        for past in reversed(recent_net):
            if score * past > 0:
                matches += 1
            else:
                break

        boosted = score * (1.0 + self.history_boost * matches)
        return max(min(boosted, 1.0), -1.0)

    def update(self, price: float, context: dict = None) -> dict:
        """
        Returns:
            {"buy": float in [0,1], "sell": float in [-1,0]}
        """
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        if not symbol:
            return {"buy": 0.0, "sell": 0.0}

        st = self._sym(symbol)
        recent_net = st["recent_net"]

        ind = ctx(context, "ind", {})
        tel = ctx(context, "tel", {})
        has_position = bool(ctx(context, "has_position", False))
        trade_info = ctx(context, "trade_info", None)
        last_sell = ctx(context, "last_sell_price", None)
        recent_max = ctx(context, "recent_max_price", price)
        atr = ctx(context, "atr", app_state["strategy"].get("atr", 0.0))
        volatility_score = ctx_float(context, "volatility_score")

        rsi_up = ind.get("rsi_momentum_up", False)
        ema_up = ind.get("ema_momentum_up", False)

        if (recent_max is None) or (abs(recent_max - price) < 1e-12):
            try:
                rp = get_recent_prices(symbol, limit=200)
                recent_max = max(rp) if rp else price
            except Exception:
                recent_max = price

        # --------------------------
        # SELL components
        # --------------------------
        sell_raw_abs = 0.0
        if has_position and trade_info:
            buy_price = float(trade_info.get("buy_price", price))
            max_price = float(trade_info.get("max_price", buy_price))
            max_price = max(max_price, buy_price)

            gain = (price - buy_price) / buy_price if buy_price else 0.0
            retrace = (max_price - price) / max_price if max_price else 0.0
            retrace_atr = retrace / atr if atr else retrace
            peak_distance_ratio = (max_price - price) / max(1e-9, (max_price - buy_price))

            buy_time = trade_info.get("buy_time")
            if isinstance(buy_time, datetime):
                holding_time = (datetime.now(timezone.utc) - buy_time).total_seconds()
            else:
                holding_time = 0.0

            hold_factor = min(1.0, max(0.25, holding_time / 120.0))
            retrace_metric = retrace_atr if atr else retrace
            sell_prox = max(0.0, min(retrace_metric / max(self.drop_from_peak, 1e-9), 1.0))

            if gain > 0 and retrace_metric > 0:
                momentum_cut = 0.5 if (rsi_up and ema_up) else (0.25 if (rsi_up or ema_up) else 0.0)
                momentum_factor = 1.0 - momentum_cut
                base_intensity = min(retrace_atr, 2.0) * 0.55 + peak_distance_ratio * 0.45
                sell_raw_abs = base_intensity * hold_factor * momentum_factor * sell_prox

            if gain < self.min_gain and not (rsi_up or ema_up):
                sell_raw_abs = max(sell_raw_abs, 0.15)

        # --------------------------
        # BUY components
        # --------------------------
        buy_raw = 0.0
        if (not has_position) and last_sell:
            drop = (last_sell - price) / last_sell if last_sell else 0.0
            reentry_drop_atr = drop / atr if atr else drop
            drop_metric = reentry_drop_atr if atr else drop

            buy_prox = max(0.0, min(drop_metric / max(self.reentry_drop, 1e-9), 1.0))

            if drop_metric > 0:
                momentum_scale = 1.0 if (ema_up and rsi_up) else (0.6 if (ema_up or rsi_up) else 0.0)
                strength = min(reentry_drop_atr, 2.0)
                vol_brake = (1.0 - min(0.6, volatility_score * 0.6))
                buy_raw = strength * buy_prox * momentum_scale * vol_brake

            if price >= recent_max * 0.998 and ema_up and rsi_up:
                buy_raw = max(buy_raw, 0.25 * (1.0 - volatility_score))

        # --------------------------
        # State-aware shaping
        # --------------------------
        vol = volatility_score
        if has_position:
            buy_raw *= 0.90
            sell_raw_abs *= 1.08
            if ema_up and rsi_up:
                sell_raw_abs *= 0.92
        else:
            buy_raw *= 1.08
            sell_raw_abs *= 0.94

        if vol > 0.8:
            buy_raw *= 0.92
            sell_raw_abs *= 0.92

        buy_raw = max(0.0, min(1.0, buy_raw))
        sell_raw = -max(0.0, min(1.0, sell_raw_abs))

        # --------------------------
        # Per-symbol history boost
        # --------------------------
        net_scalar = buy_raw - (-sell_raw)
        boosted_net = self._boost_net(symbol, net_scalar) if len(recent_net) else net_scalar
        recent_net.append(boosted_net)

        buy_prop = max(1e-6, buy_raw)
        sell_prop = max(1e-6, -sell_raw)
        total_prop = buy_prop + sell_prop
        buy_mix = buy_prop / total_prop
        sell_mix = sell_prop / total_prop

        total_intensity = min(1.0, buy_prop + sell_prop)
        target_buy = max(0.0, (boosted_net + total_intensity) / 2.0)
        target_sell_abs = max(0.0, total_intensity - target_buy)

        final_buy = 0.6 * target_buy + 0.4 * (total_intensity * buy_mix)
        final_sell_abs = 0.6 * target_sell_abs + 0.4 * (total_intensity * sell_mix)

        final_buy = float(max(0.0, min(1.0, round(final_buy, 4))))
        final_sell = float(-max(0.0, min(1.0, round(final_sell_abs, 4))))

        tel["dfp_gain"] = round(((price - trade_info.get("buy_price")) / trade_info.get("buy_price")), 5) if (has_position and trade_info and trade_info.get("buy_price")) else None
        tel["dfp_retrace_atr"] = round((((trade_info.get("max_price", price) - price) / max(1e-9, trade_info.get("max_price", price))) / atr), 4) if (has_position and trade_info and atr) else None
        tel["dfp_sell_raw"] = round(sell_raw, 4)
        tel["dfp_buy_raw"] = round(buy_raw, 4)
        tel["dfp_final_buy"] = final_buy
        tel["dfp_final_sell"] = final_sell
        tel["dfp_has_position"] = has_position
        tel["dfp_net_boosted"] = round(boosted_net, 4)

        logging.debug(
            f"[DropFromPeakSmart] {symbol} has_pos={has_position} ema↑={ema_up} rsi↑={rsi_up} "
            f"vol={vol:.2f} buy_raw={buy_raw:.3f} sell_raw={sell_raw:.3f} "
            f"→ BUY={final_buy:.3f} SELL={final_sell:.3f}"
        )

        return {"buy": final_buy, "sell": final_sell}


# === MACD Strategy (Aligned with EMA/RSI structure) ===
class MacdStrategy(BaseStrategy):
    def __init__(self, short_period=12, long_period=26, signal_period=9, min_slope=0.0002):
        self.short_period = short_period
        self.long_period = long_period
        self.signal_period = signal_period
        self.mult_short = 2 / (short_period + 1)
        self.mult_long = 2 / (long_period + 1)
        self.mult_signal = 2 / (signal_period + 1)
        self.min_slope = min_slope
        self._state_by_symbol = {}

    def _sym(self, symbol: str):
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "short_ema": None,
                "long_ema": None,
                "macd_line": None,
                "signal_line": None,
                "prev_macd": None,
                "prev_signal_line": None,
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        ind = ctx(context, "ind", {})

        if not symbol:
            return 0.0

        st = self._sym(symbol)
        price = float(f"{price:.2f}")

        if st["short_ema"] is None:
            st["short_ema"] = st["long_ema"] = price
            st["macd_line"] = 0.0
            st["signal_line"] = 0.0
            st["prev_macd"] = 0.0
            st["prev_signal_line"] = 0.0
            ind["macd_momentum_up"] = False
            return 0.0

        st["short_ema"] = (price - st["short_ema"]) * self.mult_short + st["short_ema"]
        st["long_ema"] = (price - st["long_ema"]) * self.mult_long + st["long_ema"]

        st["prev_macd"] = st["macd_line"]
        st["macd_line"] = st["short_ema"] - st["long_ema"]

        st["prev_signal_line"] = st["signal_line"]
        st["signal_line"] = (st["macd_line"] - st["signal_line"]) * self.mult_signal + st["signal_line"]

        slope = st["macd_line"] - st["prev_macd"]
        crossed_above = st["prev_macd"] < st["prev_signal_line"] and st["macd_line"] > st["signal_line"]
        crossed_below = st["prev_macd"] > st["prev_signal_line"] and st["macd_line"] < st["signal_line"]

        macd_up = (st["macd_line"] > st["signal_line"] and slope > 0) or crossed_above
        ind["macd_momentum_up"] = bool(macd_up)

        if abs(slope) < self.min_slope:
            return 0.0

        raw_score = max(min(slope * 1000, 1.0), -1.0)
        if crossed_above:
            raw_score = min(1.0, raw_score + 0.3)
        elif crossed_below:
            raw_score = max(-1.0, raw_score - 0.3)

        return round(raw_score, 4)


# === EMA Crossover Strategy ===
class LaggingEmaStrategy(BaseStrategy):
    def __init__(self, short_period=20, long_period=40, min_slope_diff=1e-8):
        self.short_period = short_period
        self.long_period = long_period
        self.mult_short = 2 / (short_period + 1)
        self.mult_long = 2 / (long_period + 1)
        self.min_slope_diff = min_slope_diff
        self._state_by_symbol = {}

    def _sym(self, symbol: str):
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "short_ema": None,
                "long_ema": None,
                "prev_short_ema": None,
                "prev_long_ema": None,
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        ind = ctx(context, "ind", {})

        if not symbol:
            return 0.0

        st = self._sym(symbol)
        price = float(f"{price:.2f}")

        if st["short_ema"] is None:
            st["short_ema"] = st["long_ema"] = price
            st["prev_short_ema"] = st["prev_long_ema"] = price
            ind["ema_slope"] = 0.0
            ind["ema_momentum_up"] = False
            return 0.0

        st["prev_short_ema"] = st["short_ema"]
        st["prev_long_ema"] = st["long_ema"]

        st["short_ema"] = (price - st["short_ema"]) * self.mult_short + st["short_ema"]
        st["long_ema"] = (price - st["long_ema"]) * self.mult_long + st["long_ema"]

        short_slope = st["short_ema"] - st["prev_short_ema"]
        long_slope = st["long_ema"] - st["prev_long_ema"]
        slope_diff = short_slope - long_slope

        ind["ema_slope"] = float(slope_diff)
        ind["ema_momentum_up"] = bool(st["short_ema"] > st["long_ema"] and short_slope > 0)

        if abs(slope_diff) < self.min_slope_diff:
            return 0.0

        score = max(min(slope_diff * 100, 1.0), -1.0)
        return round(score, 4)


# === Simple RSI Strategy ===
class LaggingRsiStrategy(BaseStrategy):
    def __init__(self, overbought=70, oversold=30, period=21, smoothing=0.025):
        self.overbought = overbought
        self.oversold = oversold
        self.period = period
        self.smoothing = smoothing
        self._state_by_symbol = {}

    def _sym(self, symbol: str):
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "gains": [],
                "losses": [],
                "prev_price": None,
                "prev_rsi": None,
                "smoothed_score": 0.0,
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        ind = ctx(context, "ind", {})

        if not symbol:
            return 0.0

        st = self._sym(symbol)
        price = float(f"{price:.2f}")

        if st["prev_price"] is None:
            st["prev_price"] = price
            ind["latest_rsi"] = 50.0
            ind["rsi_momentum_up"] = False
            return 0.0

        delta = price - st["prev_price"]
        st["prev_price"] = price

        st["gains"].append(max(delta, 0))
        st["losses"].append(abs(min(delta, 0)))

        if len(st["gains"]) > self.period:
            st["gains"].pop(0)
            st["losses"].pop(0)

        if len(st["gains"]) < self.period:
            ind["latest_rsi"] = ind.get("latest_rsi", 50.0)
            ind["rsi_momentum_up"] = bool(ind.get("rsi_momentum_up", False))
            return 0.0

        avg_gain = sum(st["gains"]) / self.period
        avg_loss = sum(st["losses"]) / self.period
        rsi = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

        if st["prev_rsi"] is not None:
            ind["rsi_momentum_up"] = bool(rsi > st["prev_rsi"])
        else:
            ind["rsi_momentum_up"] = True

        st["prev_rsi"] = rsi
        ind["latest_rsi"] = float(rsi)

        midpoint = (self.overbought + self.oversold) / 2
        range_span = self.overbought - self.oversold
        raw_score = -(rsi - midpoint) / (range_span / 2)

        clamped = max(min(raw_score, 1.0), -1.0)
        st["smoothed_score"] = st["smoothed_score"] * (1 - self.smoothing) + clamped * self.smoothing
        return round(st["smoothed_score"], 4)


# === Adaptive Hold Strategy ===
class AdaptiveHoldStrategy(BaseStrategy):
    def __init__(self, gain_threshold=0.01, loss_threshold=-0.01, min_hold=60, max_hold=600):
        self.gain_threshold = gain_threshold
        self.loss_threshold = loss_threshold
        self.min_hold = min_hold
        self.max_hold = max_hold

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        sig = ctx(context, "sig", {})
        tel = ctx(context, "tel", {})
        trade_info = ctx(context, "trade_info", None)
        has_position = bool(ctx(context, "has_position", False))

        if not symbol or not has_position or not trade_info:
            sig["min_hold_override"] = self.max_hold
            tel["adaptive_hold_score"] = 1.0
            return 1.0

        buy_price = trade_info.get("buy_price")
        buy_time = trade_info.get("buy_time")
        if not buy_price or not buy_time:
            sig["min_hold_override"] = self.max_hold
            tel["adaptive_hold_score"] = 1.0
            return 1.0

        now = datetime.now(timezone.utc)
        holding_time = (now - buy_time).total_seconds()
        price_change = (price - buy_price) / buy_price if buy_price else 0.0

        best_score = 1.0
        min_override = self.max_hold

        # === Too early to exit
        if holding_time < self.min_hold:
            sig["min_hold_override"] = self.min_hold
            tel["adaptive_hold_score"] = 1.0
            tel["adaptive_hold_seconds"] = float(holding_time)
            tel["adaptive_hold_change"] = float(price_change)
            return 1.0

        # === Strong stop loss trigger
        if price_change <= self.loss_threshold:
            score = 0.0
            logging.info(
                f"[AdaptiveHold] 🚨 Loss Exit | {symbol}: {price_change:.2%}, Held: {holding_time:.1f}s"
            )
            min_override = 0
            best_score = min(best_score, score)

        # === Profit — encourage early exit
        elif price_change >= self.gain_threshold:
            excess_gain = price_change - self.gain_threshold
            score = max(0.0, 0.8 - excess_gain * 4)
            logging.info(
                f"[AdaptiveHold] ✅ Profit Exit | {symbol}: {price_change:.2%}, Score: {score:.2f}"
            )
            min_override = 0
            best_score = min(best_score, score)

        # === Trending well, maybe early in hold — encourage patience
        elif price_change >= 0.005 and holding_time < (self.max_hold / 2):
            score = min(0.1 + price_change * 2, 0.3)
            logging.info(
                f"[AdaptiveHold] 🕒 Holding — {symbol} up {price_change:.2%}, held {holding_time:.1f}s"
            )
            best_score = max(best_score, score)

        sig["min_hold_override"] = min_override
        tel["adaptive_hold_score"] = float(round(best_score, 4))
        tel["adaptive_hold_seconds"] = float(holding_time)
        tel["adaptive_hold_change"] = float(price_change)

        return round(best_score, 4)


# --- FastEmaStrategy: add slope/accel exposure (backward-compatible) ---
class FastEmaStrategy(BaseStrategy):
    def __init__(self, short_period=8, long_period=16, max_slope_diff=0.01):
        self.short_period = short_period
        self.long_period = long_period
        self.mult_short = 2 / (short_period + 1)
        self.mult_long = 2 / (long_period + 1)
        self.max_slope_diff = max_slope_diff
        self._state_by_symbol = {}

    def _sym(self, symbol: str):
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "short_ema": None,
                "long_ema": None,
                "prev_short_ema": None,
                "prev_long_ema": None,
                "ema_slope": 0.0,
                "prev_ema_slope": 0.0,
                "ema_accel": 0.0,
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        if not symbol:
            return 0.0

        st = self._sym(symbol)
        price = float(f"{price:.2f}")

        if st["short_ema"] is None:
            st["short_ema"] = st["long_ema"] = price
            st["prev_short_ema"] = st["prev_long_ema"] = price
            st["ema_slope"] = 0.0
            st["prev_ema_slope"] = 0.0
            st["ema_accel"] = 0.0
            return 0.0

        st["prev_short_ema"] = st["short_ema"]
        st["prev_long_ema"] = st["long_ema"]

        st["short_ema"] = (price - st["short_ema"]) * self.mult_short + st["short_ema"]
        st["long_ema"] = (price - st["long_ema"]) * self.mult_long + st["long_ema"]

        short_slope = st["short_ema"] - st["prev_short_ema"]
        long_slope = st["long_ema"] - st["prev_long_ema"]
        slope_diff = short_slope - long_slope

        prev_ema_slope = st["ema_slope"]
        st["ema_slope"] = st["short_ema"] - st["long_ema"]
        st["prev_ema_slope"] = prev_ema_slope
        st["ema_accel"] = st["ema_slope"] - prev_ema_slope

        normalized = max(min(slope_diff / self.max_slope_diff, 1.0), -1.0)
        if abs(normalized) < 0.1:
            return 0.0
        return round(normalized, 4)


# --- FastRsiStrategy: expose last_rsi & rsi_slope (backward-compatible) ---
class FastRsiStrategy(BaseStrategy):
    def __init__(self, period=7, overbought=72, oversold=28, neutral_range=5):
        self.period = period
        self.overbought = overbought
        self.oversold = oversold
        self.neutral_range = neutral_range
        self._state_by_symbol = {}

    def _sym(self, symbol: str):
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "gains": [],
                "losses": [],
                "prev_price": None,
                "prev_rsi": 50.0,
                "last_rsi": 50.0,
                "rsi_slope": 0.0,
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def update(self, price: float, context: Optional[dict] = None) -> Optional[float]:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        if not symbol:
            return 0.0

        st = self._sym(symbol)
        price = float(f"{price:.2f}")

        if st["prev_price"] is None:
            st["prev_price"] = price
            st["prev_rsi"] = 50.0
            st["last_rsi"] = 50.0
            st["rsi_slope"] = 0.0
            return 0.0

        delta = price - st["prev_price"]
        st["prev_price"] = price

        st["gains"].append(max(delta, 0))
        st["losses"].append(abs(min(delta, 0)))

        if len(st["gains"]) > self.period:
            st["gains"].pop(0)
            st["losses"].pop(0)

        if len(st["gains"]) < self.period:
            return 0.0

        avg_gain = sum(st["gains"]) / self.period
        avg_loss = sum(st["losses"]) / self.period
        rsi = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

        st["rsi_slope"] = rsi - (st["last_rsi"] if st["last_rsi"] is not None else rsi)
        st["prev_rsi"] = st["last_rsi"]
        st["last_rsi"] = rsi

        midpoint = 50
        dist_from_mid = rsi - midpoint
        raw_score = dist_from_mid / 50.0

        if abs(dist_from_mid) < self.neutral_range:
            return 0.0

        return round(max(min(raw_score, 1.0), -1.0), 4)


class FastPredictiveMomentumStrategy(BaseStrategy):
    """
    Predictive momentum strategy designed to anticipate turns earlier
    than the reactive momentum cluster strategies.

    --- Purpose ---
    - Provides both buy and sell scores (dual-output model).
    - Buy score: confidence in *entering* a new position.
    - Sell score: confidence in *exiting* an existing position.
    - Both scores can be non-zero at the same time (e.g., when the market
      shows mixed predictive signals: strong uptrend but also early signs of reversal).

    --- Core Signals ---
    1. EMA acceleration (ema_accel):
       - Measures change in EMA slope.
       - Positive acceleration → strengthening uptrend (predictive buy bias).
       - Negative acceleration → weakening uptrend or strengthening downtrend
         (predictive sell bias).
    
    2. RSI slope (rsi_slope):
       - Measures change in RSI value per tick.
       - Positive slope → increasing bullish momentum.
       - Negative slope → increasing bearish momentum.
    
    3. Alignment bonus:
       - Rewards cases where short EMA aligns with predicted direction.
       - e.g., short_ema > long_ema AND acceleration positive → stronger buy signal.

    4. Extreme dampening:
       - Prevents chasing moves when RSI is already extreme.
       - If RSI > overbought, buy pressure is dampened.
       - If RSI < oversold, sell pressure is dampened.

    5. History boost:
       - Provides light persistence.
       - Sequences of same-sign signals gradually increase confidence.

    --- Predictive Behavior ---
    - Unlike cluster strategies (reactive to what’s already happening),
      this strategy projects *future continuation or reversal*.
    - Buy score may rise *before* price turns upward.
    - Sell score may rise *before* price turns downward.

    --- Usage Notes ---
    - Should be weighted more heavily when volatility is high (anticipatory edge).
    - Complements reactive strategies like MomentumClusterStrategy by offering
      an early-warning predictive layer.
    - Works best when combined with confidence aggregation so that
      predictive signals can be confirmed or vetoed by other strategies.
    """
    def __init__(
        self,
        ema_short_period=5,
        ema_long_period=8,
        rsi_period=5,
        accel_threshold=0.0004,
        rsi_slope_threshold=0.25,
        alignment_bonus=0.2,
        extreme_rsi_dampen=0.5,
        overbought=75,
        oversold=25,
        history_boost=0.08,
        history_len=8,
        flat_buy_bias=1.10,
        flat_sell_dampen=0.92,
        hold_buy_dampen=0.88,
        hold_sell_bias=1.12,
        high_vol_soften=0.90
    ):
        self.ema_short_period = ema_short_period
        self.ema_long_period = ema_long_period
        self.rsi_period = rsi_period
        self.overbought = overbought
        self.oversold = oversold

        self.accel_threshold = accel_threshold
        self.rsi_slope_threshold = rsi_slope_threshold
        self.alignment_bonus = alignment_bonus
        self.extreme_rsi_dampen = extreme_rsi_dampen

        self.history_len = history_len
        self.history_boost = history_boost

        self.flat_buy_bias = flat_buy_bias
        self.flat_sell_dampen = flat_sell_dampen
        self.hold_buy_dampen = hold_buy_dampen
        self.hold_sell_bias = hold_sell_bias
        self.high_vol_soften = high_vol_soften

        self._state_by_symbol = {}

    def _sym(self, symbol: str) -> dict:
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "ema": FastEmaStrategy(
                    short_period=self.ema_short_period,
                    long_period=self.ema_long_period
                ),
                "rsi": FastRsiStrategy(
                    period=self.rsi_period,
                    overbought=self.overbought,
                    oversold=self.oversold
                ),
                "recent_net": deque(maxlen=self.history_len),
                "prev_ema_pred_sign": 0,
                "prev_rsi_pred_sign": 0,
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def _boost_net(self, recent_net: deque, score: float) -> float:
        matches = 0
        for past in reversed(recent_net):
            if score * past > 0:
                matches += 1
            else:
                break
        boosted = score * (1.0 + self.history_boost * matches)
        return max(min(boosted, 1.0), -1.0)

    def update(self, price: float, context: dict = None) -> dict:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        tel = ctx(context, "tel", {})
        has_position = bool(ctx(context, "has_position", False))

        if not symbol:
            return {"buy": 0.0, "sell": 0.0}

        st = self._sym(symbol)
        ema = st["ema"]
        rsi = st["rsi"]
        recent_net = st["recent_net"]

        ema.update(price, context=context)
        rsi.update(price, context=context)

        ema_state = ema.get_symbol_state(symbol)
        rsi_state = rsi.get_symbol_state(symbol)

        short_ema = ema_state.get("short_ema")
        long_ema = ema_state.get("long_ema")
        ema_accel = ema_state.get("ema_accel")

        rsi_value = rsi_state.get("last_rsi")
        rsi_slope = rsi_state.get("rsi_slope")

        if any(v is None for v in (short_ema, long_ema, rsi_value, ema_accel, rsi_slope)):
            return {"buy": 0.0, "sell": 0.0}

        ema_pred = 0.0 if self.accel_threshold <= 0 else max(min(ema_accel / self.accel_threshold, 1.0), -1.0)
        rsi_pred = 0.0 if self.rsi_slope_threshold <= 0 else max(min(rsi_slope / self.rsi_slope_threshold, 1.0), -1.0)

        ema_flip_up = (st["prev_ema_pred_sign"] <= 0 and ema_pred > 0)
        ema_flip_down = (st["prev_ema_pred_sign"] >= 0 and ema_pred < 0)
        rsi_flip_up = (st["prev_rsi_pred_sign"] <= 0 and rsi_pred > 0)
        rsi_flip_down = (st["prev_rsi_pred_sign"] >= 0 and rsi_pred < 0)

        buy_dampen = 1.0
        sell_dampen = 1.0

        if rsi_value >= self.overbought:
            buy_dampen *= self.extreme_rsi_dampen
            if ema_pred < 0 or rsi_pred < 0:
                sell_dampen *= 1.05

        if rsi_value <= self.oversold:
            sell_dampen *= self.extreme_rsi_dampen
            if ema_pred > 0 or rsi_pred > 0:
                buy_dampen *= 1.05

        alignment = 0.0
        if short_ema > long_ema and (ema_pred > 0 or rsi_pred > 0):
            alignment = self.alignment_bonus
        elif short_ema < long_ema and (ema_pred < 0 or rsi_pred < 0):
            alignment = -self.alignment_bonus

        buy_from_pred = 0.70 * max(ema_pred, 0.0) + 0.30 * max(rsi_pred, 0.0)
        sell_from_pred = 0.70 * max(-ema_pred, 0.0) + 0.30 * max(-rsi_pred, 0.0)

        buy_flip_bonus = 0.0
        sell_flip_bonus = 0.0
        if ema_flip_up and rsi_flip_up:
            buy_flip_bonus = 0.20 * min(1.0, abs(ema_pred) + abs(rsi_pred))
        if ema_flip_down and rsi_flip_down:
            sell_flip_bonus = 0.20 * min(1.0, abs(ema_pred) + abs(rsi_pred))

        buy_align = max(0.0, alignment)
        sell_align = max(0.0, -alignment)

        buy_raw = (buy_from_pred + buy_flip_bonus + buy_align) * buy_dampen
        sell_raw_abs = (sell_from_pred + sell_flip_bonus + sell_align) * sell_dampen

        buy_raw = min(1.0, max(0.0, buy_raw))
        sell_raw_abs = min(1.0, max(0.0, sell_raw_abs))
        sell_raw = -sell_raw_abs

        vol = ctx_float(context, "volatility_score")
        if vol > 0.8:
            buy_raw *= self.high_vol_soften
            sell_raw *= self.high_vol_soften

        if has_position:
            buy_raw *= self.hold_buy_dampen
            sell_raw *= self.hold_sell_bias
            if ema_pred > 0.35 and rsi_pred > 0.20 and alignment > 0:
                sell_raw *= 0.90
        else:
            buy_raw *= self.flat_buy_bias
            sell_raw *= self.flat_sell_dampen

        net_scalar = buy_raw - (-sell_raw)
        boosted_net = self._boost_net(recent_net, net_scalar) if len(recent_net) else net_scalar
        recent_net.append(boosted_net)

        buy_prop = max(1e-6, buy_raw)
        sell_prop = max(1e-6, -sell_raw)
        total_prop = buy_prop + sell_prop
        buy_mix = buy_prop / total_prop
        sell_mix = sell_prop / total_prop

        total_intensity = min(1.0, buy_prop + sell_prop)
        target_buy = max(0.0, (boosted_net + total_intensity) / 2.0)
        target_sell_abs = max(0.0, total_intensity - target_buy)

        final_buy = 0.6 * target_buy + 0.4 * (total_intensity * buy_mix)
        final_sell_abs = 0.6 * target_sell_abs + 0.4 * (total_intensity * sell_mix)

        final_buy = float(max(0.0, min(1.0, round(final_buy, 4))))
        final_sell = float(-max(0.0, min(1.0, round(final_sell_abs, 4))))

        st["prev_ema_pred_sign"] = 1 if ema_pred > 0 else (-1 if ema_pred < 0 else 0)
        st["prev_rsi_pred_sign"] = 1 if rsi_pred > 0 else (-1 if rsi_pred < 0 else 0)

        tel["fast_pred_ema_accel"] = round(ema_accel, 6)
        tel["fast_pred_rsi_slope"] = round(rsi_slope, 3)
        tel["fast_pred_alignment"] = round(alignment, 3)
        tel["fast_pred_ema_pred"] = round(ema_pred, 3)
        tel["fast_pred_rsi_pred"] = round(rsi_pred, 3)
        tel["fast_pred_buy_raw"] = round(buy_raw, 4)
        tel["fast_pred_sell_raw"] = round(sell_raw, 4)
        tel["fast_pred_net_boosted"] = round(boosted_net, 4)
        tel["fast_pred_buy"] = final_buy
        tel["fast_pred_sell"] = final_sell

        return {"buy": final_buy, "sell": final_sell}

class FastMomentumClusterStrategy(BaseStrategy):
    def __init__(
        self,
        boost_factor=0.1,
        ema_weight=0.6,
        rsi_weight=0.4,
        ema_threshold=0.2,
        rsi_threshold=0.2,
        ema_short_period=8,
        ema_long_period=16,
        ema_max_slope_diff=0.01,
        rsi_period=7,
        rsi_overbought=72,
        rsi_oversold=28,
        rsi_neutral_range=5,
        recent_window=12,
        pullback_min=0.001,
        pullback_max=0.006,
        retrace_for_sell=0.0035
    ):
        self.ema_weight = ema_weight
        self.rsi_weight = rsi_weight
        self.ema_threshold = ema_threshold
        self.rsi_threshold = rsi_threshold
        self.boost_factor = boost_factor

        self.recent_window = recent_window
        self.pullback_min = pullback_min
        self.pullback_max = pullback_max
        self.retrace_for_sell = retrace_for_sell

        self.ema_short_period = ema_short_period
        self.ema_long_period = ema_long_period
        self.ema_max_slope_diff = ema_max_slope_diff
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.rsi_neutral_range = rsi_neutral_range

        self.max_bias = 0.3
        self.bias_decay = 0.95
        self._state_by_symbol = {}

    def _sym(self, symbol: str) -> dict:
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "ema": FastEmaStrategy(
                    short_period=self.ema_short_period,
                    long_period=self.ema_long_period,
                    max_slope_diff=self.ema_max_slope_diff,
                ),
                "rsi": FastRsiStrategy(
                    period=self.rsi_period,
                    overbought=self.rsi_overbought,
                    oversold=self.rsi_oversold,
                    neutral_range=self.rsi_neutral_range,
                ),
                "recent_net_scores": deque(maxlen=10),
                "prev_ema_sign": 0,
                "prev_rsi_sign": 0,
                "buy_bias": 0.0,
                "sell_bias": 0.0,
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def apply_history_boost(self, recent_net_scores: deque, score: float) -> float:
        matches = 0
        for past_score in reversed(recent_net_scores):
            if score * past_score > 0:
                matches += 1
            else:
                break
        multiplier = 1.0 + self.boost_factor * matches
        boosted = score * multiplier
        return max(min(boosted, 1.0), -1.0)

    def _pullback_from_extreme(self, recent_prices: list[float], price: float) -> tuple[float, float]:
        if len(recent_prices) < 3:
            return 0.0, 0.0

        prices = recent_prices[-self.recent_window:]
        local_max = max(prices)
        local_min = min(prices)
        pb = (local_max - price) / local_max if local_max > 0 else 0.0
        bc = (price - local_min) / local_min if local_min > 0 else 0.0
        return max(0.0, pb), max(0.0, bc)

    def update(self, price: float, context: dict = None) -> dict:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        has_position = bool(ctx(context, "has_position", False))
        tel = ctx(context, "tel", {})
        recent_prices = ctx_list(context, "recent_prices")
        volatility_score = ctx_float(context, "volatility_score")

        if not symbol:
            return {"buy": 0.0, "sell": 0.0}

        st = self._sym(symbol)
        ema = st["ema"]
        rsi = st["rsi"]

        ema_score = ema.update(price, context=context)
        rsi_score = rsi.update(price, context=context)

        ema_score = float(ema_score or 0.0)
        rsi_score = float(rsi_score or 0.0)

        tel["fast_ema_score"] = ema_score
        tel["fast_rsi_score"] = rsi_score

        alignment = (ema_score + rsi_score) / 2.0
        momentum_mag = (
            abs(ema_score) * self.ema_weight + abs(rsi_score) * self.rsi_weight
        ) / (self.ema_weight + self.rsi_weight)

        pullback_from_max, bounce_from_min = self._pullback_from_extreme(recent_prices, float(price))

        continuation = 0.0
        if ema_score > 0 and rsi_score > 0:
            cont_base = max(0.0, alignment) * (
                0.6 + 0.4 * (1.0 - min(1.0, pullback_from_max / self.pullback_max))
            )
            continuation = cont_base

        rsi_flip_up = (st["prev_rsi_sign"] <= 0 and rsi_score > 0)
        in_pullback_zone = self.pullback_min <= pullback_from_max <= self.pullback_max

        pullback_buy = 0.0
        if ema_score > 0 and rsi_flip_up and in_pullback_zone:
            zone_pos = (pullback_from_max - self.pullback_min) / max(
                1e-9, (self.pullback_max - self.pullback_min)
            )
            centered_bonus = 1.0 - abs(0.5 - zone_pos) * 1.5
            pullback_buy = min(1.0, 0.35 + 0.40 * centered_bonus)

        early_strength = 0.0
        if ema_score > self.ema_threshold or rsi_score > self.rsi_threshold:
            early_strength = 0.25 * max(ema_score, rsi_score, 0.0)

        buy_raw = max(0.0, continuation) + max(0.0, pullback_buy) + max(0.0, early_strength)
        buy_raw = min(1.0, buy_raw)

        weakness = 0.0
        if ema_score < 0 and rsi_score < 0:
            weakness = min(1.0, abs(alignment))

        ema_flip_down = (st["prev_ema_sign"] >= 0 and ema_score < 0)
        rsi_flip_down = (st["prev_rsi_sign"] >= 0 and rsi_score < 0)

        flip_down = 0.0
        if ema_flip_down and rsi_flip_down:
            flip_down = 0.4 + 0.3 * momentum_mag

        retrace = 0.0
        if pullback_from_max >= self.retrace_for_sell and rsi_score < 0:
            retrace = 0.30 + (0.20 if ema_score < 0 else 0.0)

        overextension = 0.0
        if rsi_score > 0.6 and ema_score < 0.05:
            overextension = 0.25

        sell_raw_mag = min(1.0, weakness + flip_down + retrace + overextension)
        sell_raw = -sell_raw_mag

        if volatility_score > 0.8:
            buy_raw *= 0.85
            sell_raw *= 0.85

        if has_position:
            buy_raw *= 0.80
            sell_raw *= 1.10
            if ema_score > 0.3 and rsi_score > 0.3:
                sell_raw *= 0.9
        else:
            buy_raw *= 1.10
            sell_raw *= 0.90

        if buy_raw > -sell_raw:
            st["buy_bias"] = min(
                self.max_bias,
                st["buy_bias"] * self.bias_decay + (buy_raw - (-sell_raw)) * 0.05
            )
            st["sell_bias"] *= self.bias_decay
        elif -sell_raw > buy_raw:
            st["sell_bias"] = min(
                self.max_bias,
                st["sell_bias"] * self.bias_decay + ((-sell_raw) - buy_raw) * 0.05
            )
            st["buy_bias"] *= self.bias_decay
        else:
            st["buy_bias"] *= self.bias_decay
            st["sell_bias"] *= self.bias_decay

        buy_raw = max(0.0, min(1.0, buy_raw + st["buy_bias"]))
        sell_raw = -max(0.0, min(1.0, (-sell_raw) + st["sell_bias"]))

        recent_net_scores = st["recent_net_scores"]
        net_scalar = max(buy_raw, 0.0) - max(-sell_raw, 0.0)
        boosted = self.apply_history_boost(recent_net_scores, net_scalar) if len(recent_net_scores) > 0 else net_scalar
        recent_net_scores.append(boosted)

        buy_prop = max(1e-6, buy_raw)
        sell_prop = max(1e-6, -sell_raw)
        total_prop = buy_prop + sell_prop
        buy_mix = buy_prop / total_prop
        sell_mix = sell_prop / total_prop

        total_intensity = min(1.0, buy_prop + sell_prop)
        target_buy = max(0.0, (boosted + total_intensity) / 2.0)
        target_sell_abs = max(0.0, total_intensity - target_buy)

        final_buy = 0.6 * target_buy + 0.4 * (total_intensity * buy_mix)
        final_sell_abs = 0.6 * target_sell_abs + 0.4 * (total_intensity * sell_mix)

        final_buy = float(max(0.0, min(1.0, round(final_buy, 4))))
        final_sell = float(-max(0.0, min(1.0, round(final_sell_abs, 4))))

        st["prev_ema_sign"] = 1 if ema_score > 0 else (-1 if ema_score < 0 else 0)
        st["prev_rsi_sign"] = 1 if rsi_score > 0 else (-1 if rsi_score < 0 else 0)

        tel["fmc_buy"] = final_buy
        tel["fmc_sell"] = final_sell
        tel["fmc_buy_bias"] = round(st["buy_bias"], 4)
        tel["fmc_sell_bias"] = round(st["sell_bias"], 4)

        return {"buy": final_buy, "sell": final_sell}
        

class MomentumClusterStrategy(BaseStrategy):
    def __init__(
        self,
        boost_factor=0.1,
        ema_weight=0.5,
        rsi_weight=0.3,
        macd_weight=0.2,
        macd_short_period=12,
        macd_long_period=26,
        macd_signal_period=9,
        macd_min_slope=0.0002,
        ema_short_period=20,
        ema_long_period=40,
        ema_min_slope_diff=0.00000001,
        rsi_overbought=70,
        rsi_oversold=30,
        rsi_period=21,
        rsi_smoothing=0.025,
        recent_window=24,
        pullback_min=0.0012,
        pullback_max=0.008,
        retrace_for_sell=0.004,
        bounce_for_buy=0.003,
        overextension_rsi=65,
    ):
        self.ema_weight = ema_weight
        self.rsi_weight = rsi_weight
        self.macd_weight = macd_weight

        self.boost_factor = boost_factor
        self.max_bias = 0.30
        self.bias_decay = 0.96

        self.recent_window = recent_window
        self.pullback_min = pullback_min
        self.pullback_max = pullback_max
        self.retrace_for_sell = retrace_for_sell
        self.bounce_for_buy = bounce_for_buy
        self.overextension_rsi = overextension_rsi

        self.macd_short_period = macd_short_period
        self.macd_long_period = macd_long_period
        self.macd_signal_period = macd_signal_period
        self.macd_min_slope = macd_min_slope
        self.ema_short_period = ema_short_period
        self.ema_long_period = ema_long_period
        self.ema_min_slope_diff = ema_min_slope_diff
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.rsi_period = rsi_period
        self.rsi_smoothing = rsi_smoothing

        self._state_by_symbol = {}

    def _sym(self, symbol: str) -> dict:
        symbol = clean_symbol(symbol)
        if symbol not in self._state_by_symbol:
            self._state_by_symbol[symbol] = {
                "ema": LaggingEmaStrategy(
                    short_period=self.ema_short_period,
                    long_period=self.ema_long_period,
                    min_slope_diff=self.ema_min_slope_diff,
                ),
                "rsi": LaggingRsiStrategy(
                    overbought=self.rsi_overbought,
                    oversold=self.rsi_oversold,
                    period=self.rsi_period,
                    smoothing=self.rsi_smoothing,
                ),
                "macd": MacdStrategy(
                    short_period=self.macd_short_period,
                    long_period=self.macd_long_period,
                    signal_period=self.macd_signal_period,
                    min_slope=self.macd_min_slope,
                ),
                "recent_net_scores": deque(maxlen=12),
                "buy_bias": 0.0,
                "sell_bias": 0.0,
                "prev_ema_sign": 0,
                "prev_rsi_sign": 0,
            }
        return self._state_by_symbol[symbol]

    def get_symbol_state(self, symbol: str) -> dict:
        return self._sym(symbol)

    def apply_history_boost(self, recent_net_scores: deque, score: float) -> float:
        matches = 0
        for past_score in reversed(recent_net_scores):
            if score * past_score > 0:
                matches += 1
            else:
                break
        multiplier = 1.0 + self.boost_factor * matches
        boosted = score * multiplier
        return max(min(boosted, 1.0), -1.0)

    def _window_extremes(self, recent_prices: list[float]) -> tuple[float, float, float, float]:
        if len(recent_prices) < 3:
            return 0.0, 0.0, 0.0, 0.0

        prices = recent_prices[-self.recent_window:]
        current = prices[-1]
        local_max = max(prices)
        local_min = min(prices)
        pb = (local_max - current) / local_max if local_max > 0 else 0.0
        bc = (current - local_min) / local_min if local_min > 0 else 0.0
        return max(0.0, pb), max(0.0, bc), float(local_max), float(local_min)

    def update(self, price: float, context: dict = None) -> dict:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        has_position = bool(ctx(context, "has_position", False))
        ind = ctx(context, "ind", {})
        tel = ctx(context, "tel", {})
        recent_prices = ctx_list(context, "recent_prices")

        if not symbol:
            return {"buy": 0.0, "sell": 0.0}

        st = self._sym(symbol)
        ema = st["ema"]
        rsi = st["rsi"]
        macd = st["macd"]

        ema_score = float(ema.update(price, context=context) or 0.0)
        rsi_score = float(rsi.update(price, context=context) or 0.0)
        _ = macd.update(price, context=context)

        macd_up = ind.get("macd_momentum_up", False)
        macd_score = 0.3 if macd_up else -0.3

        rsi_state = rsi.get_symbol_state(symbol)
        rsi_value = ind.get("latest_rsi", rsi_state.get("prev_rsi", 50.0) or 50.0)

        tel["ema_score"] = ema_score
        tel["rsi_score"] = rsi_score
        tel["macd_score"] = macd_score

        alignment = (ema_score + rsi_score + macd_score) / 3.0
        mag = (
            abs(ema_score) * self.ema_weight
            + abs(rsi_score) * self.rsi_weight
            + abs(macd_score) * self.macd_weight
        ) / (self.ema_weight + self.rsi_weight + self.macd_weight)

        pullback_from_max, bounce_from_min, local_max, local_min = self._window_extremes(recent_prices)

        continuation = 0.0
        if ema_score >= 0 and rsi_score >= 0 and macd_up:
            extension_penalty = min(1.0, pullback_from_max / max(self.pullback_max, 1e-9))
            continuation = max(0.0, alignment) * (0.65 + 0.35 * (1.0 - extension_penalty))

        rsi_flip_up = (st["prev_rsi_sign"] <= 0 and rsi_score > 0)
        in_pb_zone = self.pullback_min <= pullback_from_max <= self.pullback_max

        pullback_buy = 0.0
        if ema_score > 0 and rsi_flip_up and in_pb_zone and bounce_from_min >= self.bounce_for_buy:
            zone_pos = (pullback_from_max - self.pullback_min) / max(1e-9, (self.pullback_max - self.pullback_min))
            centered_bonus = 1.0 - abs(0.5 - zone_pos) * 1.5
            pullback_buy = min(1.0, 0.35 + 0.45 * centered_bonus)

        slow_blend = 0.0
        if continuation == 0.0 and pullback_buy == 0.0:
            total_w = self.ema_weight + self.rsi_weight + self.macd_weight
            slow_blend = max(
                0.0,
                (
                    ema_score * self.ema_weight
                    + rsi_score * self.rsi_weight
                    + (0.25 if macd_up else -0.15) * self.macd_weight
                ) / total_w
            )

        buy_raw = min(1.0, continuation + pullback_buy + slow_blend)

        weakness = 0.0
        if ema_score <= 0 and rsi_score <= 0 and not macd_up:
            weakness = min(1.0, abs(alignment))

        ema_flip_down = (st["prev_ema_sign"] >= 0 and ema_score < 0)
        rsi_flip_down = (st["prev_rsi_sign"] >= 0 and rsi_score < 0)

        flip_down = 0.0
        if ema_flip_down and rsi_flip_down and not macd_up:
            flip_down = 0.45 + 0.30 * mag

        retrace = 0.0
        if pullback_from_max >= self.retrace_for_sell and rsi_score < 0:
            retrace = 0.30 + (0.15 if ema_score < 0 else 0.0)
            if not macd_up:
                retrace += 0.10

        overextension = 0.0
        if rsi_value is None:
            rsi_value = 50.0

        if (rsi_value >= self.overextension_rsi) and (ema_score < 0.05) and (not macd_up):
            overextension = 0.20

        sell_mag = min(1.0, weakness + flip_down + retrace + overextension)
        sell_raw = -sell_mag

        vol = ctx_float(context, "volatility_score")
        if vol > 0.8:
            buy_raw *= 0.90
            sell_raw *= 0.90

        if has_position:
            buy_raw *= 0.90
            sell_raw *= 1.10
            if ema_score > 0.25 and rsi_score > 0.25 and macd_up:
                sell_raw *= 0.90
        else:
            buy_raw *= 1.10
            sell_raw *= 0.92

        if buy_raw > -sell_raw:
            st["buy_bias"] = min(self.max_bias, st["buy_bias"] * self.bias_decay + (buy_raw - (-sell_raw)) * 0.05)
            st["sell_bias"] *= self.bias_decay
        elif -sell_raw > buy_raw:
            st["sell_bias"] = min(self.max_bias, st["sell_bias"] * self.bias_decay + ((-sell_raw) - buy_raw) * 0.05)
            st["buy_bias"] *= self.bias_decay
        else:
            st["buy_bias"] *= self.bias_decay
            st["sell_bias"] *= self.bias_decay

        buy_raw = max(0.0, min(1.0, buy_raw + st["buy_bias"]))
        sell_raw = -max(0.0, min(1.0, (-sell_raw) + st["sell_bias"]))

        recent_net_scores = st["recent_net_scores"]
        net_scalar = max(buy_raw, 0.0) - max(-sell_raw, 0.0)
        boosted_net = self.apply_history_boost(recent_net_scores, net_scalar) if len(recent_net_scores) > 0 else net_scalar
        recent_net_scores.append(boosted_net)

        buy_prop = max(1e-6, buy_raw)
        sell_prop = max(1e-6, -sell_raw)
        total_prop = buy_prop + sell_prop
        buy_mix = buy_prop / total_prop
        sell_mix = sell_prop / total_prop

        total_intensity = min(1.0, buy_prop + sell_prop)
        target_buy = max(0.0, (boosted_net + total_intensity) / 2.0)
        target_sell_abs = max(0.0, total_intensity - target_buy)

        final_buy = 0.6 * target_buy + 0.4 * (total_intensity * buy_mix)
        final_sell_abs = 0.6 * target_sell_abs + 0.4 * (total_intensity * sell_mix)

        final_buy = float(max(0.0, min(1.0, round(final_buy, 4))))
        final_sell = float(-max(0.0, min(1.0, round(final_sell_abs, 4))))

        st["prev_ema_sign"] = 1 if ema_score > 0 else (-1 if ema_score < 0 else 0)
        st["prev_rsi_sign"] = 1 if rsi_score > 0 else (-1 if rsi_score < 0 else 0)

        tel["mc_buy"] = final_buy
        tel["mc_sell"] = final_sell
        tel["mc_buy_bias"] = round(st["buy_bias"], 4)
        tel["mc_sell_bias"] = round(st["sell_bias"], 4)

        return {"buy": final_buy, "sell": final_sell}

class OverallTrendStrategy(BaseStrategy):
    def __init__(self, ema_period=100, slope_window=20, min_slope=0.01, recheck_interval=60):
        self.ema_period = ema_period
        self.slope_window = slope_window
        self.min_slope = min_slope
        self.recheck_interval = recheck_interval
        self.last_check_by_symbol = {}
        self.last_score_by_symbol = {}

    def update(self, price: float, context: dict = None) -> float:
        context = context or {}
        symbol = ctx_symbol(context, "symbol")
        ind = ctx(context, "ind", {})
        tel = ctx(context, "tel", {})
        now = time.time()

        if not symbol:
            return 0.0

        last_check = self.last_check_by_symbol.get(symbol, 0.0)
        if now - last_check < self.recheck_interval:
            return self.last_score_by_symbol.get(symbol, 0.0)

        self.last_check_by_symbol[symbol] = now

        try:
            prices = get_recent_prices(symbol, limit=self.ema_period + self.slope_window + 5)

            if len(prices) < self.ema_period + self.slope_window:
                return 0.0

            ema = self._ema(prices, self.ema_period)
            slope = (ema[-1] - ema[-self.slope_window]) / self.slope_window
            current_price = prices[-1]
            price_position = (current_price - ema[-1]) / ema[-1] if ema[-1] else 0.0

            if slope > self.min_slope and price_position > 0:
                trend, score = "bullish", 1.0
            elif slope < -self.min_slope and price_position < 0:
                trend, score = "bearish", -1.0
            else:
                trend, score = "sideways", 0.0

            ind["overall_trend"] = trend
            tel["overall_trend_score"] = score
            self.last_score_by_symbol[symbol] = score
            return score

        except Exception as e:
            logging.error(f"[OverallTrendStrategy] Failed to compute long-term trend for {symbol}: {e}")
            return 0.0

    def _ema(self, prices: list[float], period: int) -> list[float]:
        multiplier = 2 / (period + 1)
        ema = [sum(prices[:period]) / period]
        for p in prices[period:]:
            ema.append((p - ema[-1]) * multiplier + ema[-1])
        return ema


# === Strategy Manager Class ===
class StrategyManager:
    def __init__(self, strategies, app_state, min_hold_seconds=300, required_gain=0.005, confirmation_window=3):
        self.strategies = strategies
        self.app_state = app_state
        self.min_hold_seconds = min_hold_seconds
        self.required_gain = required_gain
        self.confirmation_window = confirmation_window

        # === Signal Histories ===
        # self.signal_history_all = deque(maxlen=confirmation_window)
        # self.signal_history_lagging = deque(maxlen=confirmation_window)

        # 🧠 Tag strategies by type: used for hybrid signal confirmation logic
        self.strategy_metadata = {
            
            # === ✅ Fast momentum strategies (entry triggers) ===
            "VolumeBreakoutStrategy": {
                "speed": "fast",
                "direction": "both",
                "veto_strength": None,
                "weight_type": "signal",
                "weight": 0.9,
            },
            "PriceSurgeStrategy": {
                "speed": "fast",
                "direction": "both",
                "veto_strength": None,
                "weight_type": "signal",
                "weight": 0.7,
            },

            "FastPredictiveMomentumStrategy": {
                "speed": "fast",
                "direction": "both",
                "veto_strength": None,
                "weight_type": "signal",
                "weight": 3.6,
            },

            "FastMomentumClusterStrategy": {
                "speed": "fast",
                "direction": "both",
                "veto_strength": None,
                "weight_type": "signal",
                "weight": 2.0
            },

            # === 🧠 Lagging confirmation strategies ===
            "MomentumClusterStrategy": {
                "speed": "lagging",
                "direction": "both",
                "veto_strength": None,
                "weight_type": "signal",
                "weight": 2.8
            },
        
            "DropFromPeakSmartStrategy": {
                "speed": "lagging",
                "direction": "both",
                "veto_strength": None,
                "weight_type": "signal",
                "weight": 1.8,
            },

            # === 🚫 Hard veto brake strategies ===
            "MomentumBrakeStrategy": {
                "speed": "brake",
                "direction": "both",
                "veto_strength": "hard",
                "weight_type": "veto",
                "weight": 1,
            },
            "VolatilityReversalBrake": {
                "speed": "brake",
                "direction": "both",
                "veto_strength": "hard",
                "weight_type": "veto",
                "weight": 1,
            },
        
            # === 🛑 Soft sell-side confidence brakes ===
            "MaxUptickHoldStrategy": {
                "speed": "lagging_soft_brake",
                "direction": "sell",
                "veto_strength": "soft",
                "weight_type": "confidence_modifier",
                "weight": 1,
            },
            "VolumeSpikeStrategy": {
                "speed": "final_soft_brake",
                "direction": "buy",
                "veto_strength": "soft",
                "weight_type": "confidence_modifier",
                "weight": 1,
                "floor": 0.45
            },
            "VolumeMomentumFilterStrategy": {
                "speed": "filter",
                "direction": "buy",
                "veto_strength": "soft",
                "weight_type": "confidence_modifier",
                "weight": 1,
                "floor": 0.45,
            },
            "TopRejectionStrategy": {
                "speed": "filter",
                "direction": "buy",
                "veto_strength": "soft",
                "weight_type": "confidence_modifier",
                "weight": 1,
            },
            "OverboughtSoftBrakeStrategy": {
                "speed": "filter",
                "direction": "buy",
                "veto_strength": "soft",
                "weight_type": "confidence_modifier",
                "weight": 1,
            },
        
            # === ⚠️ Trade filters (volatility, trend, and dynamic hold logic) ===
            "AdaptiveHoldStrategy": {
                "speed": "filter",
                "direction": "sell",
                "veto_strength": None,
                "weight_type": "confidence_modifier",
                "weight": 1,
            },
            "BasicVolatilityFilter": {
                "speed": "filter",
                "direction": "both",
                "veto_strength": None,
                "weight_type": "confidence_modifier",
                "weight": 1,
            },
            "SmartTrendFilterStrategy": {
                "speed": "filter",
                "direction": "both",
                "veto_strength": None,
                "weight_type": "confidence_modifier",
                "weight": 1,
            },
        }
                
        self.confidence_model = ConfidenceModel(app_state, self.strategy_metadata)
        app_state["strategy"]["confidence_model"] = self.confidence_model

    @property
    def fail_safe_active(self) -> bool:
        # Fast-path: event is set (no lock needed just to check it)
        if fail_safe_event.is_set():
            return True
        # Fallback: read the dict under a lock for consistency
        with app_state_lock:
            return bool(self.app_state.get("fail_safes", {}).get("state", False))
    
    # ─────────────────────────────────────────────
    # evaluate_trade() decision flow
    # ─────────────────────────────────────────────
    #
    # High-level stages:
    #   1. Normalize symbol and gather per-symbol market context
    #   2. Update volatility / ATR / indicator state
    #   3. Run all strategies and collect raw outputs
    #   4. Apply hard veto strategies
    #   5. Blend signal strategies into buy/sell confidence
    #   6. Compare confidence vs thresholds with conflict arbitration
    #   7. Apply execution gates (cooldown, min_hold, can_sell, etc.)
    #   8. Return final action: "buy", "sell", or None
    #
    # Important concepts:
    #   strategy_results:
    #       Raw output from each strategy. Can be:
    #         - scalar score in [-1, 1]
    #         - dual-channel dict: {"buy": x, "sell": y}
    #         - confidence modifier in [0, 1]
    #
    #   raw_signals:
    #       Compact list of signal strategies that were directionally active
    #       for this evaluation. Used mainly for logging / diagnostics.
    #
    #   vetoes:
    #       Names of hard veto strategies or execution blockers that prevented
    #       a trade from being taken.
    #
    #   buy_conf / sell_conf:
    #       Final blended confidence values after:
    #         - signal weighting
    #         - fast/lagging regime blend
    #         - filter modifiers
    #         - soft-brake penalties
    #         - EMA smoothing
    #
    #   buy_edge / sell_edge:
    #       Directional dominance scores used for conflict resolution.
    #       buy_edge  = buy_conf - abs(sell_conf)
    #       sell_edge = abs(sell_conf) - buy_conf
    #
    #   decision_stage:
    #       candidate          -> evaluation in progress
    #       vetoed             -> blocked by hard veto
    #       threshold_pass     -> a buy or sell signal won arbitration
    #       no_signal          -> no side passed / won arbitration
    #       execution_blocked  -> signal existed but execution gate blocked it
    #       execution_override -> forced action (e.g. failsafe)
    #       executed           -> final buy/sell returned
    #       skipped            -> evaluated but no trade taken
    #
    # Notes:
    #   - "has_position" determines whether we are looking for entry or exit.
    #   - "can_sell" is an execution gate, not a strategy opinion.
    #   - This function is intentionally verbose because it is the main bridge
    #     between strategy logic and execution logic.

    def evaluate_trade(self, price, has_position, can_sell, symbol):
        symbol = clean_symbol(symbol)
        if not symbol:
            return None, {}

        price = float(price)

        strategy_results = {}
        veto_triggered = False
        vetoes = []

        # ─────────────────────────────────────────────
        # Pull per-symbol buffers once
        # ─────────────────────────────────────────────
        strategy_cfg = self.app_state["strategy"]["config"]

        recent_prices = list(
            get_recent_prices(symbol, limit=strategy_cfg["price_window"]) or []
        )
        recent_volumes = list(
            get_recent_volumes(symbol, limit=strategy_cfg["volume_window"]) or []
        )
        price_points = get_recent_price_points(symbol, limit=strategy_cfg["price_window"])
        volume_points = get_recent_volume_points(symbol, limit=strategy_cfg["volume_window"])

        recent_max_price = max(recent_prices, default=price)

        # ─────────────────────────────────────────────
        # Per-symbol volatility
        # ─────────────────────────────────────────────
        vol_scorer = self.app_state["strategy"].get("volatility_scorer")
        volatility_score = float(vol_scorer.score(symbol)) if (vol_scorer and symbol) else 0.5

        # ─────────────────────────────────────────────
        # Update per-symbol ATR
        # ─────────────────────────────────────────────
        atr_filter = self.app_state["strategy"].get("atr_filter")
        context_atr = 0.0

        if atr_filter and len(recent_prices) >= 2:
            try:
                latest_close = float(recent_prices[-1])
                latest_prev = float(recent_prices[-2])
                latest_high = max(latest_close, latest_prev)
                latest_low = min(latest_close, latest_prev)

                atr_filter.update(symbol, latest_high, latest_low, latest_close)

                atr_value = atr_filter.get_atr(symbol)
                context_atr = float(atr_value) if atr_value is not None else 0.0
            except Exception:
                logging.exception("[StrategyManager] Failed to update ATR filter for %s", symbol)

        # ─────────────────────────────────────────────
        # Per-symbol state bucket
        # ─────────────────────────────────────────────
        sym_state = ensure_symbol_strategy_state(self.app_state, symbol)
        ind = sym_state["ind"]
        sig = sym_state["sig"]
        tel = sym_state["tel"]

        ind["volatility_score"] = volatility_score
        tel["volatility_score"] = volatility_score
        tel["recent_max_price"] = recent_max_price

        trade_info = self.app_state.get("open_trades", {}).get(symbol)
        last_sell_price = self.app_state["strategy"].get("last_sell_price_by_symbol", {}).get(symbol)

        context = {
            "symbol": symbol,
            "atr": context_atr,
            "sym_state": sym_state,
            "ind": ind,
            "sig": sig,
            "tel": tel,
            "trade_info": trade_info,
            "last_sell_price": last_sell_price,
            "has_position": has_position,
            "can_sell": can_sell,
            "recent_max_price": recent_max_price,
            "recent_prices": recent_prices,
            "recent_volumes": recent_volumes,
            "price_points": price_points,
            "volume_points": volume_points,
            "volatility_score": volatility_score,
        }

        # ─────────────────────────────────────────────
        # Debug
        # ─────────────────────────────────────────────
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.debug(
                "[StrategyManager] Context snapshot | %s | prices=%d volumes=%d vol_score=%.4f",
                symbol,
                len(recent_prices),
                len(recent_volumes),
                volatility_score,
            )
            logging.debug(
                "[StrategyManager] price_tail=%s volume_tail=%s",
                recent_prices[-5:],
                recent_volumes[-5:],
            )

            try:
                buf = self.app_state.get("market_data", {}).get("buffer")
                if buf is not None:
                    logging.debug("[MarketDataBuffer] snapshot=%s", buf.snapshot())
            except Exception:
                logging.exception("[StrategyManager] Failed to snapshot market buffer")

        # ─────────────────────────────────────────────
        # Run strategies
        # ─────────────────────────────────────────────
        for strategy in self.strategies:
            name = strategy.__class__.__name__

            try:
                logging.debug("[StrategyManager] Running %s for %s", name, symbol)

                result = strategy.update(price, context=context)

                if isinstance(result, (np.floating, np.integer)):
                    result = float(result)

                strategy_results[name] = result
                logging.debug("[%s] Score: %s", name, result)

                meta = self.strategy_metadata.get(name, {})
                if meta.get("veto_strength") == "hard":
                    if isinstance(result, (int, float)) and float(result) != 1.0:
                        veto_triggered = True
                        vetoes.append(name)
                        logging.info("[StrategyManager] 🚫 Hard veto from %s → Trade blocked", name)

            except Exception as e:
                logging.exception("[StrategyManager] ❌ Error in strategy %s: %s", name, e)

        try:
            compact_results = {}
            for name, result in strategy_results.items():
                if isinstance(result, dict):
                    compact_results[name] = {
                        k: round(float(v), 4) if isinstance(v, (int, float)) else v
                        for k, v in result.items()
                    }
                elif isinstance(result, (int, float)):
                    compact_results[name] = round(float(result), 4)
                else:
                    compact_results[name] = result

            logging.info("[StrategyResults] %s | %s", symbol, compact_results)
        except Exception:
            logging.exception("[StrategyResults] Failed to format strategy results for %s", symbol)

        # ─────────────────────────────────────────────
        # Raw signal list
        # ─────────────────────────────────────────────
        raw_signals = []

        for name, result in strategy_results.items():
            meta = self.strategy_metadata.get(name, {})
            if meta.get("weight_type") != "signal":
                continue

            try:
                if isinstance(result, dict):
                    if result.get("buy", 0) > 0:
                        raw_signals.append(f"{name}:buy")
                    if result.get("sell", 0) < 0:
                        raw_signals.append(f"{name}:sell")
                elif isinstance(result, (int, float)):
                    if result > 0:
                        raw_signals.append(f"{name}:buy")
                    elif result < 0:
                        raw_signals.append(f"{name}:sell")
            except Exception:
                pass

        # ─────────────────────────────────────────────
        # Hard veto path
        # ─────────────────────────────────────────────
        decision_stage = "candidate"

        if veto_triggered:
            decision_stage = "vetoed"
            try:
                sig.setdefault("history_all", []).append("brake")
                sig.setdefault("history_lagging", []).append("brake")
            except Exception:
                pass

            tel["decision_stage"] = decision_stage

            log_trade_decision(
                symbol,
                "buy" if not has_position else "sell",
                price,
                "vetoed",
                confidence=None,
                triggers=raw_signals,
                vetoes=vetoes,
                volatility=volatility_score,
                strategy_results=strategy_results,
                decision_stage=decision_stage,
                decision_reason="hard_veto",
                buy_conf=None,
                sell_conf=None,
                buy_edge=None,
                sell_edge=None,
                buy_threshold=None,
                sell_threshold=None,
            )
            return None, strategy_results

        # ─────────────────────────────────────────────
        # Confidence scoring
        # ─────────────────────────────────────────────
        buy_conf, sell_conf = self.confidence_model.get_confidence(
            strategy_results,
            symbol=symbol,
            context=context,
        )

        sig["last_buy_confidence"] = float(buy_conf)
        sig["last_sell_confidence"] = float(sell_conf)
        tel["last_buy_confidence"] = float(buy_conf)
        tel["last_sell_confidence"] = float(sell_conf)

        tel["buy_conf"] = float(buy_conf)
        tel["sell_conf"] = float(sell_conf)

        logging.debug("[ConfidenceModel] %s 🔢 BuyConf: %.2f | SellConf: %.2f", symbol, buy_conf, sell_conf)

        # ─────────────────────────────────────────────
        # Final signal with conflict arbitration
        # ─────────────────────────────────────────────
        signal = None
        decision_reason = "none"

        buy_th = float(self.app_state.get("config_defaults", {}).get("BUY_CONFIDENCE_THRESHOLD", 0.40))
        sell_th = float(self.app_state.get("config_defaults", {}).get("SELL_CONFIDENCE_THRESHOLD", -0.40))

        # Slightly stricter entry in calmer conditions.
        # This helps reduce mediocre flat-market buys.
        if volatility_score < 0.35:
            buy_th += 0.03

        # Slightly faster exits in highly volatile conditions.
        if volatility_score > 0.80:
            sell_th += 0.03   # e.g. -0.50 -> -0.47

        buy_crossed = buy_conf >= buy_th
        sell_crossed = sell_conf <= sell_th

        # How much one side dominates the other
        conflict_margin = self.app_state.get("config_defaults", {}).get("CONFIDENCE_CONFLICT_MARGIN", 0.12)

        buy_strength = float(buy_conf)
        sell_strength = abs(float(sell_conf))

        buy_edge = buy_strength - sell_strength
        sell_edge = sell_strength - buy_strength

        tel["buy_edge"] = float(round(buy_edge, 4))
        tel["sell_edge"] = float(round(sell_edge, 4))
        tel["buy_threshold"] = float(round(buy_th, 4))
        tel["sell_threshold"] = float(round(sell_th, 4))

        logging.info(
            "[Decision] %s | buy_conf=%.4f sell_conf=%.4f buy_th=%.4f sell_th=%.4f "
            "buy_crossed=%s sell_crossed=%s buy_edge=%.4f sell_edge=%.4f "
            "conflict_margin=%.4f vetoes=%s raw_signals=%s",
            symbol,
            buy_conf,
            sell_conf,
            buy_th,
            sell_th,
            buy_crossed,
            sell_crossed,
            buy_edge,
            sell_edge,
            conflict_margin,
            vetoes,
            raw_signals,
        )

        if not has_position:
            if buy_crossed and not sell_crossed:
                signal = "buy"
                decision_reason = "buy_only"

            elif buy_crossed and sell_crossed:
                if buy_edge >= conflict_margin:
                    signal = "buy"
                    decision_reason = "buy_wins_conflict"
                else:
                    signal = None
                    decision_reason = "conflict_blocked_flat"

            else:
                signal = None
                decision_reason = "no_flat_entry"

        else:
            if sell_crossed and not buy_crossed:
                signal = "sell"
                decision_reason = "sell_only"

            elif buy_crossed and sell_crossed:
                if sell_edge >= conflict_margin:
                    signal = "sell"
                    decision_reason = "sell_wins_conflict"
                else:
                    signal = None
                    decision_reason = "conflict_blocked_in_position"

            else:
                signal = None
                decision_reason = "no_exit"

        if signal is None:
            decision_stage = "no_signal"
        else:
            decision_stage = "threshold_pass"

        tel["decision_stage"] = decision_stage
        tel["decision_reason"] = decision_reason

        logging.info(
            "[DecisionResult] %s | signal=%s reason=%s buy_conf=%.4f sell_conf=%.4f "
            "buy_edge=%.4f sell_edge=%.4f",
            symbol,
            signal,
            decision_reason,
            buy_conf,
            sell_conf,
            buy_edge,
            sell_edge,
        )

        # ─────────────────────────────────────────────
        # Cooldown
        # ─────────────────────────────────────────────
        now = time.time()
        cooldown_until = float(sig.get("cooldown_until", 0.0))

        if now < cooldown_until:
            logging.info("[Cooldown] %s trading cooldown active until %.0f", symbol, cooldown_until)
            log_trade_decision(
                symbol,
                signal,
                price,
                "blocked",
                confidence=(buy_conf if signal == "buy" else sell_conf),
                triggers=raw_signals,
                vetoes=["cooldown"],
                volatility=volatility_score,
                strategy_results=strategy_results,
                decision_stage="execution_blocked",
                decision_reason="cooldown",
                buy_conf=buy_conf,
                sell_conf=sell_conf,
                buy_edge=buy_edge,
                sell_edge=sell_edge,
                buy_threshold=buy_th,
                sell_threshold=sell_th,
            )
            return None, strategy_results

        # ─────────────────────────────────────────────
        # Failsafe override
        # ─────────────────────────────────────────────
        if self.fail_safe_active:
            log_trade_decision(
                symbol,
                "sell",
                price,
                "executed",
                confidence=sell_conf,
                triggers=raw_signals,
                vetoes=["failsafe_trigger"],
                volatility=volatility_score,
                strategy_results=strategy_results,
                decision_stage="execution_override",
                decision_reason="failsafe_trigger",
                buy_conf=buy_conf,
                sell_conf=sell_conf,
                buy_edge=buy_edge,
                sell_edge=sell_edge,
                buy_threshold=buy_th,
                sell_threshold=sell_th,
            )

            with app_state_lock:
                fs = self.app_state.setdefault("fail_safes", {})
                fs["state"] = False
                fs["cleared_at"] = time.time()

            fail_safe_event.clear()
            return "sell", strategy_results

        # ─────────────────────────────────────────────
        # Buy path
        # ─────────────────────────────────────────────
        if signal == "buy" and not has_position:
            log_trade_decision(
                symbol,
                "buy",
                price,
                "executed",
                confidence=buy_conf,
                triggers=raw_signals,
                vetoes=[],
                volatility=volatility_score,
                strategy_results=strategy_results,
                decision_stage="executed",
                decision_reason=decision_reason,
                buy_conf=buy_conf,
                sell_conf=sell_conf,
                buy_edge=buy_edge,
                sell_edge=sell_edge,
                buy_threshold=buy_th,
                sell_threshold=sell_th,
            )
            return "buy", strategy_results

        # ─────────────────────────────────────────────
        # Sell path
        # ─────────────────────────────────────────────
        if signal == "sell":
            if not has_position:
                logging.debug("[Sell Skipped] No position in open_trades for %s", symbol)
                log_trade_decision(
                    symbol,
                    "sell",
                    price,
                    "blocked",
                    confidence=sell_conf,
                    triggers=raw_signals,
                    vetoes=["no_position"],
                    volatility=volatility_score,
                    strategy_results=strategy_results,
                    decision_stage="execution_blocked",
                    decision_reason="no_position",
                    buy_conf=buy_conf,
                    sell_conf=sell_conf,
                    buy_edge=buy_edge,
                    sell_edge=sell_edge,
                    buy_threshold=buy_th,
                    sell_threshold=sell_th,
                )
                return None, strategy_results

            if can_sell and has_position:
                trade_info = self.app_state.get("open_trades", {}).get(symbol)
                if not trade_info:
                    logging.warning("⚠️ %s: No trade info found for held position.", symbol)
                    log_trade_decision(
                        symbol,
                        "sell",
                        price,
                        "blocked",
                        confidence=sell_conf,
                        triggers=raw_signals,
                        vetoes=["missing_trade_info"],
                        volatility=volatility_score,
                        strategy_results=strategy_results,
                        decision_stage="execution_blocked",
                        decision_reason="missing_trade_info",
                        buy_conf=buy_conf,
                        sell_conf=sell_conf,
                        buy_edge=buy_edge,
                        sell_edge=sell_edge,
                        buy_threshold=buy_th,
                        sell_threshold=sell_th,
                    )
                    return None, strategy_results

                buy_time = trade_info.get("buy_time")
                held_duration = (datetime.now(timezone.utc) - buy_time).total_seconds() if buy_time else 0.0

                dynamic_hold = float(
                    sig.get(
                        "min_hold_override",
                        getattr(
                            self,
                            "min_hold_seconds",
                            self.app_state.get("strategy", {}).get("min_hold_seconds", 1),
                        ),
                    )
                )

                if held_duration < dynamic_hold:
                    logging.info(
                        "⏸ %s: Sell blocked (min_hold) Held %.1fs < min %ss.",
                        symbol,
                        held_duration,
                        dynamic_hold,
                    )
                    log_trade_decision(
                        symbol,
                        "sell",
                        price,
                        "blocked",
                        confidence=sell_conf,
                        triggers=raw_signals,
                        vetoes=["min_hold"],
                        volatility=volatility_score,
                        strategy_results=strategy_results,
                        decision_stage="execution_blocked",
                        decision_reason="min_hold",
                        buy_conf=buy_conf,
                        sell_conf=sell_conf,
                        buy_edge=buy_edge,
                        sell_edge=sell_edge,
                        buy_threshold=buy_th,
                        sell_threshold=sell_th,
                    )
                    return None, strategy_results

                log_trade_decision(
                    symbol,
                    "sell",
                    price,
                    "executed",
                    confidence=sell_conf,
                    triggers=raw_signals,
                    vetoes=[],
                    volatility=volatility_score,
                    strategy_results=strategy_results,
                    decision_stage="executed",
                    decision_reason=decision_reason,
                    buy_conf=buy_conf,
                    sell_conf=sell_conf,
                    buy_edge=buy_edge,
                    sell_edge=sell_edge,
                    buy_threshold=buy_th,
                    sell_threshold=sell_th,
                )
                return "sell", strategy_results

            log_trade_decision(
                symbol,
                "sell",
                price,
                "blocked",
                confidence=sell_conf,
                triggers=raw_signals,
                vetoes=["cannot_sell"],
                volatility=volatility_score,
                strategy_results=strategy_results,
                decision_stage="execution_blocked",
                decision_reason="cannot_sell",
                buy_conf=buy_conf,
                sell_conf=sell_conf,
                buy_edge=buy_edge,
                sell_edge=sell_edge,
                buy_threshold=buy_th,
                sell_threshold=sell_th,
            )
            return None, strategy_results

        if signal is None:
            log_trade_decision(
                symbol,
                "",
                price,
                "skipped",
                confidence=None,
                triggers=raw_signals,
                vetoes=vetoes,
                volatility=volatility_score,
                strategy_results=strategy_results,
                decision_stage=decision_stage,
                decision_reason=decision_reason,
                buy_conf=buy_conf,
                sell_conf=sell_conf,
                buy_edge=buy_edge,
                sell_edge=sell_edge,
                buy_threshold=buy_th,
                sell_threshold=sell_th,
            )

        return None, strategy_results

# Strategy order matters mainly for readability and diagnostics.
# Hard vetoes / filters are listed early, then fast entry logic,
# then lagging confirmation logic. Confidence blending itself is
# controlled by strategy_metadata, not by this list order.

# === Global Strategy Manager Instance ===
strategy_manager = StrategyManager(
    strategies=[
        VolatilityReversalBrake(),
        BasicVolatilityFilter(),
        SmartTrendFilterStrategy(),
        VolumeSpikeStrategy(),
        VolumeBreakoutStrategy(),
        PriceSurgeStrategy(window=3, threshold_pct=1.2),
        MaxUptickHoldStrategy(max_hold_seconds=18, min_profit=0.001, max_loss=-0.004),
        MomentumBrakeStrategy(soft_block_rate=0.005),
        VolumeMomentumFilterStrategy(),
        # MicroRangeRejectionStrategy(window=5, threshold=0.002),
        OverboughtSoftBrakeStrategy(),
        TopRejectionStrategy(),
        DropFromPeakSmartStrategy(min_gain=0.0001, drop_from_peak=0.0005, reentry_drop=0.002),
        AdaptiveHoldStrategy(gain_threshold=0.004, loss_threshold=-0.002, min_hold=8, max_hold=45),
        FastPredictiveMomentumStrategy(),
        FastMomentumClusterStrategy(),
        MomentumClusterStrategy()
        # LaggingEmaStrategy(),
        # LaggingRsiStrategy(),
        # MacdStrategy()
    ],
    app_state=app_state,
    min_hold_seconds=10,
    required_gain=-0.0001,
    confirmation_window=3
)
