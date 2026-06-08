from dataclasses import dataclass
from typing import Dict


@dataclass
class ProposedTrade:
    symbol: str
    action: str
    current_weight: float
    target_weight: float
    difference: float
    reason: str


class PaperPortfolio:
    def __init__(self):
        self.weights: Dict[str, float] = {"CASH": 1.0}

    def compare_to_target(self, target: Dict[str, float], min_trade_diff: float = 0.05):
        proposed = []

        symbols = set(self.weights.keys()) | set(target.keys())

        for symbol in symbols:
            current_weight = self.weights.get(symbol, 0.0)
            target_weight = target.get(symbol, 0.0)
            diff = target_weight - current_weight

            if abs(diff) < min_trade_diff:
                continue

            if symbol == "CASH":
                continue

            action = "BUY" if diff > 0 else "SELL"

            proposed.append(
                ProposedTrade(
                    symbol=symbol,
                    action=action,
                    current_weight=current_weight,
                    target_weight=target_weight,
                    difference=diff,
                    reason=f"{symbol}: current={current_weight:.2%}, target={target_weight:.2%}",
                )
            )

        return proposed

    def apply_target(self, target: Dict[str, float]):
        self.weights = dict(target)