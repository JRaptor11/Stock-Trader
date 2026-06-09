from dataclasses import dataclass
from typing import Dict, List, Optional
import statistics
import logging


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
            if len(bars) < 20:
                continue

            closes = [b["close"] for b in bars]
            volumes = [b["volume"] for b in bars]
            trade_counts = [b.get("trade_count", 0.0) for b in bars]

            last_price = closes[-1]

            ret_20 = self._pct_return(closes, 20) or 0.0
            ret_10 = self._pct_return(closes, 10) or 0.0

            ema_10 = self._ema(closes, 10)
            ema_20 = self._ema(closes, 20)

            trend_score = 0.0
            if ema_10 and ema_20:
                trend_score = (ema_10 - ema_20) / ema_20

            recent_vol = sum(volumes[-4:]) / 4
            base_vol = sum(volumes[-20:]) / 20
            volume_score = 0.0 if base_vol <= 0 else max(-0.25, min(0.25, recent_vol / base_vol - 1.0))

            recent_trades = sum(trade_counts[-4:]) / 4
            base_trades = sum(trade_counts[-20:]) / 20
            trade_count_score = 0.0 if base_trades <= 0 else max(-0.25, min(0.25, recent_trades / base_trades - 1.0))

            volatility = self._volatility_penalty(closes, lookback=20)

            score = (
                0.40 * ret_20
                + 0.25 * ret_10
                + 0.20 * trend_score
                + 0.075 * volume_score
                + 0.075 * trade_count_score
                - 0.10 * volatility
            )

            reason = (
                f"ret_20={ret_20:.4f}, "
                f"ret_10={ret_10:.4f}, "
                f"trend={trend_score:.4f}, "
                f"volume={volume_score:.4f}, "
                f"trade_count={trade_count_score:.4f}, "
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
    ):
        self.top_n = top_n
        self.min_cash_pct = min_cash_pct
        self.max_cash_pct = max_cash_pct
        self.min_position_pct = min_position_pct
        self.max_position_pct = max_position_pct
        self.score_epsilon = score_epsilon

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

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

    def _cash_allocation(self, selected: List[StockScore]) -> tuple[float, str]:
        if not selected:
            return 1.0, "no_candidates"

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

        return cash_pct, market_strength

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

    def build_target_portfolio(self, ranked_scores: List[StockScore]) -> Dict[str, float]:
        selected = ranked_scores[: self.top_n]

        if not selected:
            return {
                "CASH": 1.0,
                "_meta": {
                    "market_strength": "no_candidates",
                    "avg_top_score": None,
                    "top_score": None,
                    "cash_pct": 1.0,
                    "investable_pct": 0.0,
                    "weighting_mode": "none",
                },
            }

        cash_pct, market_strength = self._cash_allocation(selected)
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
            "avg_top_score": round(avg_score, 4),
            "top_score": round(top_score, 4),
            "score_spread": round(score_spread, 4),
            "cash_pct": round(cash_pct, 4),
            "investable_pct": round(investable_pct, 4),
            "weighting_mode": "score_weighted",
        }

        return target


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

        return {
            "ranked": ranked,
            "target_portfolio": target,
        }