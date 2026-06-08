from dataclasses import dataclass
from typing import Dict, List


@dataclass
class StockScore:
    symbol: str
    score: float
    last_price: float
    reason: str


class Layer1StockRanker:
    def __init__(self, market_data_buffer):
        self.market_data_buffer = market_data_buffer

    def score_symbol(self, symbol: str) -> StockScore | None:
        prices = self.market_data_buffer.get_recent_prices(symbol, limit=60)
        volumes = self.market_data_buffer.get_recent_volumes(symbol, limit=60)

        if len(prices) < 20:
            return None

        last_price = prices[-1]
        ret_5 = (prices[-1] - prices[-5]) / prices[-5] if len(prices) >= 5 and prices[-5] else 0
        ret_20 = (prices[-1] - prices[-20]) / prices[-20] if prices[-20] else 0

        vol_score = 0
        if len(volumes) >= 20:
            recent_vol = sum(volumes[-5:]) / 5
            base_vol = sum(volumes[-20:]) / 20
            vol_score = (recent_vol / base_vol - 1) if base_vol else 0

        score = (0.6 * ret_20) + (0.3 * ret_5) + (0.1 * vol_score)

        reason = f"ret_20={ret_20:.4f}, ret_5={ret_5:.4f}, vol_score={vol_score:.4f}"

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