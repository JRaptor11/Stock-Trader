from dataclasses import dataclass
from typing import Dict, List, Optional
import statistics
import logging
import math


@dataclass
class StockScore:
    symbol: str
    score: float
    last_price: float
    reason: str


class Layer1StockRanker:
    def __init__(self, market_data_buffer):
        self.market_data_buffer = market_data_buffer

    @staticmethod
    def _pct_return(prices: List[float], lookback: int) -> Optional[float]:
        if len(prices) <= lookback:
            return None
        start = prices[-lookback]
        end = prices[-1]
        if start == 0:
            return None
        return (end - start) / start

    @staticmethod
    def _ema(prices: List[float], period: int) -> Optional[float]:
        if len(prices) < period:
            return None

        k = 2 / (period + 1)
        ema = prices[-period]

        for price in prices[-period + 1:]:
            ema = price * k + ema * (1 - k)

        return ema

    @staticmethod
    def _volatility_penalty(prices: List[float], lookback: int = 40) -> float:
        if len(prices) < lookback + 1:
            return 0.0

        returns = []
        recent = prices[-lookback:]

        for i in range(1, len(recent)):
            if recent[i - 1] != 0:
                returns.append((recent[i] - recent[i - 1]) / recent[i - 1])

        if len(returns) < 2:
            return 0.0

        return statistics.stdev(returns)

    @staticmethod
    def _volume_confirmation(volumes: List[float]) -> float:
        if len(volumes) < 60:
            return 0.0

        recent_vol = sum(volumes[-10:]) / 10
        base_vol = sum(volumes[-60:]) / 60

        if base_vol <= 0:
            return 0.0

        ratio = recent_vol / base_vol

        # Clamp so volume confirms price action but cannot dominate.
        return max(-0.25, min(0.25, ratio - 1.0))

    def rank_from_bars(self, bars_by_symbol: Dict[str, list]) -> List[StockScore]:
        scores = []

        for symbol, bars in bars_by_symbol.items():
            # With 5-minute bars:
            # 60 bars ≈ 5 trading hours.
            # Require 61 so ret_60 can compare current close to 60 bars ago.
            if len(bars) < 61:
                continue

            closes = [b["close"] for b in bars]
            volumes = [b["volume"] for b in bars]
            trade_counts = [b.get("trade_count", 0.0) for b in bars]

            last_price = closes[-1]

            # 5-minute equivalents of the old 15-minute windows:
            # old ret_20 on 15m bars ≈ 300 minutes ≈ new ret_60 on 5m bars
            # old ret_10 on 15m bars ≈ 150 minutes ≈ new ret_30 on 5m bars
            ret_60 = self._pct_return(closes, 60) or 0.0
            ret_30 = self._pct_return(closes, 30) or 0.0

            ema_30 = self._ema(closes, 30)
            ema_60 = self._ema(closes, 60)

            trend_score = 0.0
            if ema_30 and ema_60:
                trend_score = (ema_30 - ema_60) / ema_60

            # Old recent 4 bars on 15m ≈ 60 minutes.
            # New recent 12 bars on 5m ≈ 60 minutes.
            recent_vol = sum(volumes[-12:]) / 12
            base_vol = sum(volumes[-60:]) / 60
            volume_ratio = 0.0 if base_vol <= 0 else recent_vol / base_vol
            volume_score = (
                0.0
                if volume_ratio <= 0
                else max(-0.25, min(0.25, math.log(volume_ratio) / 3.0))
            )

            recent_trades = sum(trade_counts[-12:]) / 12
            base_trades = sum(trade_counts[-60:]) / 60
            trade_count_ratio = 0.0 if base_trades <= 0 else recent_trades / base_trades
            trade_count_score = (
                0.0
                if trade_count_ratio <= 0
                else max(-0.25, min(0.25, math.log(trade_count_ratio) / 3.0))
            )

            volatility = self._volatility_penalty(closes, lookback=60)

            direction_score = (
                0.50 * ret_60
                + 0.30 * ret_30
                + 0.20 * trend_score
            )

            activity_score = (
                0.50 * volume_score
                + 0.50 * trade_count_score
            )

            activity_multiplier = 1.0 + max(-0.25, min(0.25, activity_score))

            score = (
                direction_score * activity_multiplier
                - 0.10 * volatility
            )

            reason = (
                f"ret_60={ret_60:.4f}, "
                f"ret_30={ret_30:.4f}, "
                f"trend={trend_score:.4f}, "
                f"direction={direction_score:.4f}, "
                f"volume_ratio={volume_ratio:.4f}, "
                f"volume={volume_score:.4f}, "
                f"trade_count_ratio={trade_count_ratio:.4f}, "
                f"trade_count={trade_count_score:.4f}, "
                f"activity_mult={activity_multiplier:.4f}, "
                f"volatility={volatility:.4f}, "
                f"bars={len(bars)}"
            )

            scores.append(
                StockScore(
                    symbol=symbol,
                    score=score,
                    last_price=last_price,
                    reason=reason,
                )
            )

        return sorted(scores, key=lambda x: x.score, reverse=True)

    def score_symbol(self, symbol: str) -> Optional[StockScore]:
        prices = self.market_data_buffer.get_recent_prices(symbol, limit=120)
        volumes = self.market_data_buffer.get_recent_volumes(symbol, limit=120)

        if len(prices) < 60:
            return None

        last_price = prices[-1]

        ret_20 = self._pct_return(prices, 20) or 0.0
        ret_60 = self._pct_return(prices, 60) or 0.0

        ema_20 = self._ema(prices, 20)
        ema_60 = self._ema(prices, 60)

        if ema_20 is None or ema_60 is None or ema_60 == 0:
            trend_score = 0.0
        else:
            trend_score = (ema_20 - ema_60) / ema_60

        volume_score = self._volume_confirmation(volumes)
        volatility = self._volatility_penalty(prices, lookback=40)

        score = (
            0.45 * ret_60
            + 0.30 * ret_20
            + 0.20 * trend_score
            + 0.05 * volume_score
            - 0.10 * volatility
        )

        reason = (
            f"ret_60={ret_60:.4f}, "
            f"ret_20={ret_20:.4f}, "
            f"trend={trend_score:.4f}, "
            f"volume={volume_score:.4f}, "
            f"volatility={volatility:.4f}"
        )

        return StockScore(
            symbol=symbol,
            score=score,
            last_price=last_price,
            reason=reason,
        )

    def rank(self, symbols: List[str]) -> List[StockScore]:
        scores = []

        for symbol in symbols:
            result = self.score_symbol(symbol)
            if result is not None:
                scores.append(result)

        return sorted(scores, key=lambda x: x.score, reverse=True)


class Layer2PortfolioBuilder:
    def __init__(
        self,
        top_n: int = 5,
        min_cash_pct: float = 0.05,
        max_cash_pct: float = 0.70,
        min_position_pct: float = 0.05,
        max_position_pct: float = 0.30,
        score_epsilon: float = 0.001,

        # Adaptive target smoothing.
        target_smoothing_enabled: bool = True,
        normal_alpha: float = 0.30,
        normal_max_step: float = 0.05,
        shock_alpha: float = 0.60,
        shock_max_step: float = 0.15,
        risk_off_alpha: float = 0.80,
        risk_off_max_step: float = 0.25,
        shock_threshold: float = 0.10,
        min_smoothed_position_pct: float = 0.005,
    ):
        self.top_n = top_n
        self.min_cash_pct = min_cash_pct
        self.max_cash_pct = max_cash_pct
        self.min_position_pct = min_position_pct
        self.max_position_pct = max_position_pct
        self.score_epsilon = score_epsilon

        self.target_smoothing_enabled = target_smoothing_enabled
        self.normal_alpha = normal_alpha
        self.normal_max_step = normal_max_step
        self.shock_alpha = shock_alpha
        self.shock_max_step = shock_max_step
        self.risk_off_alpha = risk_off_alpha
        self.risk_off_max_step = risk_off_max_step
        self.shock_threshold = shock_threshold
        self.min_smoothed_position_pct = min_smoothed_position_pct

        self.previous_target_portfolio: Dict[str, float] | None = None

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    @staticmethod
    def _tradable_weights(target: Dict[str, float]) -> Dict[str, float]:
        if not isinstance(target, dict):
            return {}

        out = {}

        for symbol, weight in target.items():
            symbol_str = str(symbol or "").upper().strip()

            if not symbol_str:
                continue

            if symbol_str.startswith("_"):
                continue

            if symbol_str in {"CASH", "USD"}:
                continue

            try:
                weight_float = float(weight or 0.0)
            except Exception:
                weight_float = 0.0

            if weight_float > 0:
                out[symbol_str] = weight_float

        return out

    def _market_strength_label(self, avg_score: float) -> str:
        if avg_score >= 0.04:
            return "strong"
        if avg_score >= 0.02:
            return "healthy"
        if avg_score >= 0.00:
            return "mixed_positive"
        if avg_score >= -0.02:
            return "weak"
        return "very_weak"

    def _cash_allocation(self, selected: List[StockScore]) -> tuple[float, str, float | None]:
        if not selected:
            return 1.0, "no_candidates", None

        avg_score = sum(s.score for s in selected) / len(selected)
        market_strength = self._market_strength_label(avg_score)

        weak_score = -0.04
        strong_score = 0.04

        strength_factor = (avg_score - weak_score) / (strong_score - weak_score)
        strength_factor = self._clamp(strength_factor, 0.0, 1.0)

        cash_pct = self.max_cash_pct - (
            strength_factor * (self.max_cash_pct - self.min_cash_pct)
        )

        cash_pct = self._clamp(cash_pct, self.min_cash_pct, self.max_cash_pct)

        return cash_pct, market_strength, strength_factor

    def _normalize_with_constraints(
        self,
        raw_weights: Dict[str, float],
        investable_pct: float,
    ) -> Dict[str, float]:
        remaining_symbols = set(raw_weights.keys())
        final_weights = {}
        remaining_allocation = investable_pct

        while remaining_symbols:
            total_raw = sum(raw_weights[symbol] for symbol in remaining_symbols)

            if total_raw <= 0:
                equal_weight = remaining_allocation / len(remaining_symbols)
                proposed = {
                    symbol: equal_weight
                    for symbol in remaining_symbols
                }
            else:
                proposed = {
                    symbol: remaining_allocation * (raw_weights[symbol] / total_raw)
                    for symbol in remaining_symbols
                }

            locked_this_round = False

            for symbol in list(remaining_symbols):
                weight = proposed[symbol]

                if weight < self.min_position_pct:
                    final_weights[symbol] = self.min_position_pct
                    remaining_allocation -= self.min_position_pct
                    remaining_symbols.remove(symbol)
                    locked_this_round = True

                elif weight > self.max_position_pct:
                    final_weights[symbol] = self.max_position_pct
                    remaining_allocation -= self.max_position_pct
                    remaining_symbols.remove(symbol)
                    locked_this_round = True

            if not locked_this_round:
                for symbol in remaining_symbols:
                    final_weights[symbol] = proposed[symbol]
                break

            if remaining_allocation <= 0:
                break

        total = sum(final_weights.values())

        if total > 0 and abs(total - investable_pct) > 0.000001:
            scale = investable_pct / total
            final_weights = {
                symbol: weight * scale
                for symbol, weight in final_weights.items()
            }

        return final_weights

    def _build_raw_target_portfolio(self, ranked_scores: List[StockScore]) -> Dict[str, float]:
        selected = ranked_scores[: self.top_n]

        if not selected:
            return {
                "CASH": 1.0,
                "_meta": {
                    "market_strength": "no_candidates",
                    "strength_factor": None,
                    "avg_top_score": None,
                    "top_score": None,
                    "score_spread": None,
                    "cash_pct": 1.0,
                    "investable_pct": 0.0,
                    "weighting_mode": "none",
                },
            }

        cash_pct, market_strength, strength_factor = self._cash_allocation(selected)
        investable_pct = 1.0 - cash_pct

        scores = [s.score for s in selected]
        min_score = min(scores)
        max_score = max(scores)
        score_spread = max_score - min_score

        raw_weights = {
            s.symbol: max(
                (s.score - min_score) + self.score_epsilon,
                self.score_epsilon,
            )
            for s in selected
        }

        target = self._normalize_with_constraints(
            raw_weights=raw_weights,
            investable_pct=investable_pct,
        )

        target = {
            symbol: round(weight, 4)
            for symbol, weight in target.items()
        }

        target["CASH"] = round(cash_pct, 4)

        avg_score = sum(scores) / len(scores)
        top_score = selected[0].score

        target["_meta"] = {
            "market_strength": market_strength,
            "strength_factor": round(strength_factor, 4) if strength_factor is not None else None,
            "avg_top_score": round(avg_score, 4),
            "top_score": round(top_score, 4),
            "score_spread": round(score_spread, 4),
            "cash_pct": round(cash_pct, 4),
            "investable_pct": round(investable_pct, 4),
            "weighting_mode": "score_weighted",
        }

        return target

    def _select_smoothing_profile(
        self,
        raw_target: Dict[str, float],
        previous_target: Dict[str, float],
    ) -> tuple[float, float, str, float]:
        raw_weights = self._tradable_weights(raw_target)
        previous_weights = self._tradable_weights(previous_target)

        symbols = set(raw_weights.keys()) | set(previous_weights.keys()) | {"CASH"}

        max_raw_change = 0.0
        for symbol in symbols:
            raw_weight = float(raw_target.get(symbol, 0.0) or 0.0)
            previous_weight = float(previous_target.get(symbol, 0.0) or 0.0)
            max_raw_change = max(max_raw_change, abs(raw_weight - previous_weight))

        market_strength = (
            raw_target.get("_meta", {}).get("market_strength")
            if isinstance(raw_target.get("_meta"), dict)
            else None
        )

        if market_strength in {"weak", "very_weak", "no_candidates"}:
            return (
                self.risk_off_alpha,
                self.risk_off_max_step,
                "risk_off_fast",
                max_raw_change,
            )

        if max_raw_change >= self.shock_threshold:
            return (
                self.shock_alpha,
                self.shock_max_step,
                "large_change_fast",
                max_raw_change,
            )

        return (
            self.normal_alpha,
            self.normal_max_step,
            "normal_smooth",
            max_raw_change,
        )

    def _apply_adaptive_smoothing(self, raw_target: Dict[str, float]) -> Dict[str, float]:
        if not self.target_smoothing_enabled:
            raw_target["_meta"]["smoothing_applied"] = False
            raw_target["_meta"]["smoothing_mode"] = "disabled"
            self.previous_target_portfolio = dict(raw_target)
            return raw_target

        previous_target = self.previous_target_portfolio

        if not previous_target:
            raw_target["_meta"]["smoothing_applied"] = False
            raw_target["_meta"]["smoothing_mode"] = "first_target"
            self.previous_target_portfolio = dict(raw_target)
            return raw_target

        alpha, max_step, smoothing_mode, max_raw_change = self._select_smoothing_profile(
            raw_target=raw_target,
            previous_target=previous_target,
        )

        raw_weights = self._tradable_weights(raw_target)
        previous_weights = self._tradable_weights(previous_target)

        symbols = sorted(set(raw_weights.keys()) | set(previous_weights.keys()))

        smoothed_weights = {}

        for symbol in symbols:
            previous_weight = previous_weights.get(symbol, 0.0)
            raw_weight = raw_weights.get(symbol, 0.0)

            desired_weight = previous_weight + alpha * (raw_weight - previous_weight)

            lower_bound = max(0.0, previous_weight - max_step)
            upper_bound = previous_weight + max_step

            smoothed_weight = self._clamp(
                desired_weight,
                lower_bound,
                upper_bound,
            )

            if smoothed_weight >= self.min_smoothed_position_pct:
                smoothed_weights[symbol] = smoothed_weight

        position_total = sum(smoothed_weights.values())
        max_investable = 1.0 - self.min_cash_pct

        if position_total > max_investable and position_total > 0:
            scale = max_investable / position_total
            smoothed_weights = {
                symbol: weight * scale
                for symbol, weight in smoothed_weights.items()
            }
            position_total = sum(smoothed_weights.values())

        cash_pct = max(0.0, 1.0 - position_total)

        final_target = {
            symbol: round(weight, 4)
            for symbol, weight in smoothed_weights.items()
        }
        final_target["CASH"] = round(cash_pct, 4)

        raw_meta = raw_target.get("_meta", {})
        if not isinstance(raw_meta, dict):
            raw_meta = {}

        raw_symbol_weights = {
            symbol: round(weight, 4)
            for symbol, weight in raw_weights.items()
        }

        final_target["_meta"] = {
            **raw_meta,
            "smoothing_applied": True,
            "smoothing_mode": smoothing_mode,
            "smoothing_alpha": round(alpha, 4),
            "smoothing_max_step": round(max_step, 4),
            "max_raw_weight_change": round(max_raw_change, 4),
            "raw_cash_pct": round(float(raw_target.get("CASH", 0.0) or 0.0), 4),
            "smoothed_cash_pct": round(cash_pct, 4),
            "raw_symbol_weights": raw_symbol_weights,
            "smoothed_symbol_count": len(smoothed_weights),
        }

        self.previous_target_portfolio = dict(final_target)

        return final_target

    def build_target_portfolio(self, ranked_scores: List[StockScore]) -> Dict[str, float]:
        raw_target = self._build_raw_target_portfolio(ranked_scores)
        return self._apply_adaptive_smoothing(raw_target)


class LayeredPortfolioEngine:
    def __init__(self, market_data_buffer, top_n: int = 5):
        self.ranker = Layer1StockRanker(market_data_buffer)
        self.portfolio_builder = Layer2PortfolioBuilder(top_n=top_n)

    def evaluate(self, symbols: List[str], bars_by_symbol: Dict[str, list] | None = None) -> dict:
        if bars_by_symbol is not None:
            ranked = self.ranker.rank_from_bars(bars_by_symbol)
        else:
            ranked = self.ranker.rank(symbols)

        target = self.portfolio_builder.build_target_portfolio(ranked)

        return {
            "ranked": ranked,
            "target_portfolio": target,
        }