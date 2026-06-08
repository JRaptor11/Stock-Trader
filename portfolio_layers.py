from dataclasses import dataclass
from typing import Dict, List, Optional
import statistics


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
    def __init__(self, top_n: int = 5, cash_buffer_pct: float = 0.05):
        self.top_n = top_n
        self.cash_buffer_pct = cash_buffer_pct

    def build_target_portfolio(self, ranked_scores: List[StockScore]) -> Dict[str, float]:
        selected = ranked_scores[: self.top_n]

        if not selected:
            return {"CASH": 1.0}

        investable_pct = 1.0 - self.cash_buffer_pct
        weight = investable_pct / len(selected)

        target = {score.symbol: weight for score in selected}
        target["CASH"] = self.cash_buffer_pct

        return target


class LayeredPortfolioEngine:
    def __init__(self, market_data_buffer, top_n: int = 5):
        self.ranker = Layer1StockRanker(market_data_buffer)
        self.portfolio_builder = Layer2PortfolioBuilder(top_n=top_n)

    def evaluate(self, symbols: List[str]) -> dict:
        ranked = self.ranker.rank(symbols)
        target = self.portfolio_builder.build_target_portfolio(ranked)

        return {
            "ranked": ranked,
            "target_portfolio": target,
        }