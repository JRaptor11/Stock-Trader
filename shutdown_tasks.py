# shutdown_tasks.py

import asyncio
import inspect
import logging
import threading

from state import app_state
from utils.lifecycle_utils import (
    record_program_shutdown,
    safe_close_trading_client,
)


async def shutdown_trading_bot() -> None:
    """
    Cleanly shut down background tasks, streams, services, clients,
    Telegram, and local lifecycle state.
    """

    try:
        app_state["stream"]["shutdown_event"].set()
    except Exception:
        pass

    try:
        tasks = list(app_state["main"]["async_tasks"])

        for task in tasks:
            task.cancel()

        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=10,
                )
            except asyncio.TimeoutError:
                logging.warning("⚠️ Timed out waiting for background async tasks to cancel.")

        app_state["main"]["async_tasks"].clear()

    except Exception as e:
        logging.warning("⚠️ Error cancelling async tasks: %s", e)

    logging.info("⏹ Attempting to stop data stream...")
    stream = app_state["stream"].get("manager")

    if stream:
        try:
            if inspect.iscoroutinefunction(getattr(stream, "stop", None)):
                await asyncio.wait_for(stream.stop(), timeout=15)
            else:
                await asyncio.wait_for(asyncio.to_thread(stream.stop), timeout=15)

        except asyncio.TimeoutError:
            logging.warning("⚠️ Stream stop timed out.")

        except Exception as e:
            logging.error("❌ Error stopping stream: %s", e)

        finally:
            app_state["stream"]["running"] = False

            stream_thread = app_state["stream"].get("thread")
            if not stream_thread or not stream_thread.is_alive():
                app_state["stream"]["thread"] = None
                app_state["stream"]["loop"] = None
                app_state["stream"]["instance"] = None
                app_state["stream"]["manager"] = None
                app_state["stream"]["state"] = "stopped"

    try:
        position_tracker = app_state["services"].get("position_tracker", {}).get("instance")
        balance_tracker = app_state["services"].get("balance_tracker", {}).get("instance")

        if position_tracker:
            position_tracker.stop()

        if balance_tracker:
            balance_tracker.stop_periodic_updates()

    except Exception:
        pass

    if app_state.get("trading_client"):
        await safe_close_trading_client(app_state["trading_client"])

    try:
        telegram_state = app_state.get("telegram", {})
        tg_app = telegram_state.get("bot_app")
        tg_task = telegram_state.get("task")

        if tg_app:
            try:
                if getattr(tg_app, "updater", None):
                    await asyncio.wait_for(tg_app.updater.stop(), timeout=5)
            except Exception:
                logging.warning("[TelegramBot] updater.stop() failed (ignored).", exc_info=True)

            try:
                await asyncio.wait_for(tg_app.stop(), timeout=5)
            except Exception:
                logging.warning("[TelegramBot] app.stop() failed (ignored).", exc_info=True)

            try:
                await asyncio.wait_for(tg_app.shutdown(), timeout=5)
            except Exception:
                logging.warning("[TelegramBot] app.shutdown() failed (ignored).", exc_info=True)

        if tg_task:
            tg_task.cancel()
            try:
                await asyncio.wait_for(tg_task, timeout=5)
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.warning(
                    "[TelegramBot] telegram task cancel/wait failed (ignored).",
                    exc_info=True,
                )

    except Exception:
        logging.warning("Telegram shutdown failed (ignored).", exc_info=True)

    finally:
        app_state["stream"]["running"] = False

        stream_thread = app_state["stream"].get("thread")
        if not stream_thread or not stream_thread.is_alive():
            app_state["stream"]["instance"] = None
            app_state["stream"]["thread"] = None
            app_state["stream"]["loop"] = None
            app_state["stream"]["state"] = "stopped"

    try:
        threads = list(app_state["main"].get("threads", []))

        for thread in threads:
            if thread and getattr(thread, "is_alive", lambda: False)():
                thread.join(timeout=2)

    except Exception:
        logging.warning("⚠️ Error joining background threads (ignored).", exc_info=True)

    app_state["main"]["threads"].clear()

    record_program_shutdown(reason="clean")

    try:
        logging.info(
            "🧵 Threads still alive: "
            + ", ".join(thread.name for thread in threading.enumerate())
        )
    except Exception:
        pass

    try:
        loop = asyncio.get_running_loop()
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        pretty = []

        for task in pending:
            try:
                name = task.get_name()
            except Exception:
                name = str(task)

            try:
                coro = task.get_coro()
                coro_name = getattr(coro, "__qualname__", repr(coro))
            except Exception:
                coro_name = "unknown_coro"

            pretty.append(f"{name}->{coro_name}")

        logging.info(
            "🌀 Pending asyncio tasks (%s): %s",
            len(pending),
            " | ".join(pretty[:12]),
        )

    except Exception:
        pass

    logging.info("👋 Shutdown complete")