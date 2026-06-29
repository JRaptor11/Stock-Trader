# layer1_ranker.py

from dataclasses import dataclass
from typing import Dict, List, Optional
import statistics
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

