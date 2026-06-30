# layer2_portfolio.py

from typing import Dict, List

from layers.layer1_ranker import StockScore, Layer1StockRanker


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


class Layer2PortfolioEngine:
    """
    Runs Layer 1 and Layer 2 together.

    Layer 1:
        Rank candidate symbols.

    Layer 2:
        Convert ranked symbols into a target portfolio.
    """

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