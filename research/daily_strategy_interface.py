"""Shared contract for independently evaluated daily research strategies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class DailyStrategyContext:
    histories: Mapping[str, list[float]]
    config: Any


@dataclass(frozen=True)
class DailyStrategySpec:
    name: str
    concept_family: str
    target_builder: Callable[[DailyStrategyContext], Mapping[str, float]]
    rebalance_frequency: str | None = None
    signal_timing: str = "session_close"
    execution_timing: str = "next_session_open"
    version: int = 1

    def __post_init__(self):
        if not self.name or not self.concept_family:
            raise ValueError("strategy name and concept family are required")
        if self.rebalance_frequency not in (None, "daily", "weekly", "monthly"):
            raise ValueError("unsupported strategy rebalance frequency")


class DailyStrategyRegistry:
    def __init__(self, specs):
        specs = tuple(specs)
        self._specs = {spec.name: spec for spec in specs}
        if len(self._specs) != len(specs):
            raise ValueError("daily strategy names must be unique")

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def cadence(self, name: str, default: str) -> str:
        return self.spec(name).rebalance_frequency or default

    def spec(self, name: str) -> DailyStrategySpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ValueError(f"unknown daily strategy: {name}") from exc

    def targets(self, name: str, histories: Mapping[str, list[float]], config: Any) -> dict[str, float]:
        raw = dict(self.spec(name).target_builder(DailyStrategyContext(histories, config)))
        allowed = set(histories) | {str(config.cash_proxy_symbol)}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"strategy {name} returned symbols outside its research universe: {sorted(unknown)}")
        targets = {str(symbol): float(weight) for symbol, weight in raw.items()}
        if any(not math.isfinite(weight) or weight < 0 for weight in targets.values()):
            raise ValueError(f"strategy {name} returned non-finite or negative weights")
        if sum(targets.values()) > 1.0 + 1e-9:
            raise ValueError(f"strategy {name} returned weights above 100%")
        return targets

    def snapshot(self, selected: tuple[str, ...] | None = None) -> list[dict]:
        names = selected or self.names()
        return [
            {
                "name": spec.name,
                "concept_family": spec.concept_family,
                "rebalance_frequency": spec.rebalance_frequency or "job_default",
                "signal_timing": spec.signal_timing,
                "execution_timing": spec.execution_timing,
                "interface_version": spec.version,
            }
            for spec in (self.spec(name) for name in names)
        ]
