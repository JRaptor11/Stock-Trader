"""Fail-closed process identity and broker-execution policy."""

from __future__ import annotations

import os
from enum import Enum


class ServiceMode(str, Enum):
    PAPER_TRADING = "paper_trading"
    HISTORICAL_RESEARCH = "historical_research"


def _strict_bool(name: str, environ: dict[str, str]) -> bool:
    raw = environ.get(name)
    if raw is None:
        raise RuntimeError(f"{name} must be explicitly set to true or false")
    normalized = raw.strip().lower()
    if normalized not in {"true", "false"}:
        raise RuntimeError(f"{name} must be exactly true or false, got {raw!r}")
    return normalized == "true"


def service_mode(environ: dict[str, str] | None = None) -> ServiceMode:
    env = os.environ if environ is None else environ
    raw = str(env.get("SERVICE_MODE") or "").strip().lower()
    try:
        return ServiceMode(raw)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in ServiceMode)
        raise RuntimeError(
            f"SERVICE_MODE must be explicitly set to one of: {allowed}"
        ) from exc


def validate_service_startup(
    expected: ServiceMode,
    environ: dict[str, str] | None = None,
) -> ServiceMode:
    """Validate process identity and execution policy before startup work."""
    env = os.environ if environ is None else environ
    actual = service_mode(env)
    if actual is not expected:
        raise RuntimeError(
            f"Wrong entry point for SERVICE_MODE={actual.value!r}; "
            f"this process requires {expected.value!r}"
        )

    execution_enabled = _strict_bool("BROKER_EXECUTION_ENABLED", env)
    required = expected is ServiceMode.PAPER_TRADING
    if execution_enabled is not required:
        raise RuntimeError(
            f"BROKER_EXECUTION_ENABLED must be {str(required).lower()} "
            f"for SERVICE_MODE={expected.value}"
        )
    return actual
