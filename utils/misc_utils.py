"""
import asyncio
import logging
import time
import inspect
import random
from functools import wraps
"""
# === RETRY DECORATOR ===
"""
def with_retries(retries=3, delay=2):
    def decorator(func):
        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                last_exception = None
                for attempt in range(retries):
                    try:
                        return await func(*args, **kwargs)
                    except Exception as e:
                        last_exception = e
                        logging.warning(f"Async retry {attempt+1}/{retries} for {func.__name__} failed: {e}")
                        await asyncio.sleep(delay)
                logging.error(f"All async retries failed for {func.__name__}.")
                return {"error": str(last_exception)}, 500
            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    logging.warning(f"Sync retry {attempt+1}/{retries} for {func.__name__} failed: {e}")

                    # ✅ avoid blocking an active event loop if we happen to be in one
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None

                    if loop and loop.is_running():
                        # we’re in an async environment; do a non-blocking sleep safely
                        # NOTE: this blocks only *this* call path until sleep finishes, but doesn't freeze the loop
                        fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(delay), loop)
                        fut.result()
                    else:
                        time.sleep(delay)

            logging.error(f"All sync retries failed for {func.__name__}.")
            return {"error": str(last_exception)}, 500

        return sync_wrapper
    return decorator
"""

# utils/misc_utils.py
import asyncio
import logging
import inspect
import random
import time
from functools import wraps
from typing import Callable, Iterable, Optional, Tuple, Type


def with_retries(
    retries: int = 3,
    delay: float = 2.0,
    *,
    backoff: float = 2.0,
    max_delay: float = 30.0,
    jitter: float = 0.25,
    retry_on: Tuple[Type[BaseException], ...] = (TimeoutError, ConnectionError, OSError),
    on_fail: Optional[Callable[[str, BaseException], None]] = None,
    raise_on_fail: bool = False,
):
    """
    Retry decorator for both sync and async functions.

    What you get (safe defaults for network-ish operations):
      - Exponential backoff (delay *= backoff each attempt) capped at max_delay
      - Jitter (random +/- jitter*delay) to avoid retry storms
      - Only retries exceptions in `retry_on` (avoids retrying logic bugs)
      - Optional `on_fail(func_name, last_exception)` callback after all retries
      - Optional `raise_on_fail` to re-raise last exception after retries

    Notes:
      - Sync wrapper uses time.sleep (blocking) by design.
      - Async wrapper uses asyncio.sleep (non-blocking).
      - If you need to call a sync function from async code, do it at the call site with:
            await asyncio.to_thread(sync_func, ...)
    """

    # basic validation/safety
    retries = max(1, int(retries))
    delay = float(delay)
    backoff = float(backoff)
    max_delay = float(max_delay)
    jitter = float(jitter)

    def _compute_sleep(base: float) -> float:
        base = max(0.0, float(base))
        if jitter <= 0:
            return base
        # jitter in range [base*(1-jitter), base*(1+jitter)]
        lo = base * (1.0 - jitter)
        hi = base * (1.0 + jitter)
        return max(0.0, random.uniform(lo, hi))

    def decorator(func):
        func_name = getattr(func, "__name__", "unknown")

        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                last_exception: Optional[BaseException] = None
                current_delay = delay

                for attempt in range(1, retries + 1):
                    try:
                        return await func(*args, **kwargs)

                    except retry_on as e:
                        last_exception = e
                        if attempt >= retries:
                            break
                        sleep_s = _compute_sleep(current_delay)
                        logging.warning(
                            f"[with_retries] Async retry {attempt}/{retries} for {func_name} failed: {e} "
                            f"(sleep {sleep_s:.2f}s)"
                        )
                        await asyncio.sleep(sleep_s)
                        current_delay = min(current_delay * backoff, max_delay)

                    except Exception as e:
                        # Non-retriable exceptions (logic bugs, etc.)
                        logging.exception(
                            f"[with_retries] Async non-retriable error in {func_name}: {e}"
                        )
                        raise

                # exhausted retries
                if last_exception is None:
                    last_exception = RuntimeError("Retry wrapper exhausted without exception (unexpected).")

                logging.error(f"[with_retries] All async retries failed for {func_name}: {last_exception}")

                if callable(on_fail):
                    try:
                        on_fail(func_name, last_exception)
                    except Exception:
                        logging.warning("[with_retries] on_fail callback raised (ignored).", exc_info=True)

                if raise_on_fail:
                    raise last_exception

                return {"error": str(last_exception)}, 500

            return async_wrapper

        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                last_exception: Optional[BaseException] = None
                current_delay = delay

                for attempt in range(1, retries + 1):
                    try:
                        return func(*args, **kwargs)

                    except retry_on as e:
                        last_exception = e
                        if attempt >= retries:
                            break
                        sleep_s = _compute_sleep(current_delay)
                        logging.warning(
                            f"[with_retries] Sync retry {attempt}/{retries} for {func_name} failed: {e} "
                            f"(sleep {sleep_s:.2f}s)"
                        )
                        time.sleep(sleep_s)
                        current_delay = min(current_delay * backoff, max_delay)

                    except Exception as e:
                        # Non-retriable exceptions (logic bugs, etc.)
                        logging.exception(
                            f"[with_retries] Sync non-retriable error in {func_name}: {e}"
                        )
                        raise

                # exhausted retries
                if last_exception is None:
                    last_exception = RuntimeError("Retry wrapper exhausted without exception (unexpected).")

                logging.error(f"[with_retries] All sync retries failed for {func_name}: {last_exception}")

                if callable(on_fail):
                    try:
                        on_fail(func_name, last_exception)
                    except Exception:
                        logging.warning("[with_retries] on_fail callback raised (ignored).", exc_info=True)

                if raise_on_fail:
                    raise last_exception

                return {"error": str(last_exception)}, 500

            return sync_wrapper

    return decorator