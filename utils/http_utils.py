"""Small HTTP client safety helpers."""

from __future__ import annotations


def configure_default_request_timeout(client, timeout) -> bool:
    """Apply a default timeout to a requests-backed SDK client.

    Alpaca-py 0.40.1 does not pass a timeout to ``requests.Session``. This
    wrapper keeps explicit caller timeouts intact and is idempotent so startup
    retries cannot stack wrappers.
    """
    session = getattr(client, "_session", None)
    request = getattr(session, "request", None)
    if session is None or not callable(request):
        return False

    session._stock_trader_default_timeout = timeout

    if getattr(session, "_stock_trader_timeout_wrapped", False):
        return True

    original_request = request

    def request_with_default_timeout(method, url, **kwargs):
        kwargs.setdefault(
            "timeout",
            session._stock_trader_default_timeout,
        )
        return original_request(method, url, **kwargs)

    session.request = request_with_default_timeout
    session._stock_trader_timeout_wrapped = True
    return True
