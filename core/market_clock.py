import logging


def get_market_is_open(app_state: dict) -> bool:
    """
    Prefer broker clock. Fall back to market_monitor state.
    """
    client = app_state.get("trading_client")

    if client is not None:
        try:
            clock = client.get_clock()
            return bool(getattr(clock, "is_open", False))
        except Exception:
            logging.warning("[MarketClock] Could not fetch market clock.", exc_info=True)

    try:
        return bool(
            app_state
            .get("services", {})
            .get("market_monitor", {})
            .get("market_open", False)
        )
    except Exception:
        return False