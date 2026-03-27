import asyncio
import json
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.error import Conflict
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from routes.public_routes import uptime_health_check
from routes.admin_routes import (
    start_stream,
    shutdown_stream,
    stream_status,
    metrics,
    health_check,
    force_alert,
    get_all_config,
)
from routes.dev_routes import (
    test_positions,
    test_alert,
    test_telegram_alert,
    run_diagnostics,
    view_trade_log,
)

from . import config_utils as config
from state import app_state


telegram_start_lock = asyncio.Lock()


def get_allowed_chat_ids() -> list[int]:
    raw_ids = getattr(config, "TELEGRAM_CHAT_ID", None)

    if raw_ids is None:
        return []

    if isinstance(raw_ids, str):
        ids = []
        for part in raw_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError:
                logging.warning(
                    "[TelegramBot] Skipping invalid TELEGRAM_CHAT_ID entry: %r",
                    part,
                )
        return ids

    if isinstance(raw_ids, (list, tuple, set)):
        ids = []
        for item in raw_ids:
            try:
                ids.append(int(str(item).strip()))
            except (ValueError, TypeError):
                logging.warning(
                    "[TelegramBot] Skipping invalid TELEGRAM_CHAT_ID entry: %r",
                    item,
                )
        return ids

    try:
        return [int(raw_ids)]
    except (ValueError, TypeError):
        logging.warning("[TelegramBot] Invalid TELEGRAM_CHAT_ID value: %r", raw_ids)
        return []


def is_allowed_chat(update: Update) -> bool:
    allowed_ids = get_allowed_chat_ids()
    chat_id = update.effective_chat.id if update.effective_chat else None

    if chat_id is None:
        return False

    if chat_id not in allowed_ids:
        logging.warning(
            "[TelegramBot] Unauthorized chat attempted command. chat_id=%s allowed_count=%s",
            chat_id,
            len(allowed_ids),
        )
        return False

    return True


def is_shutdown_in_progress() -> bool:
    try:
        shutdown_event = app_state.get("stream", {}).get("shutdown_event")
        return bool(shutdown_event and shutdown_event.is_set())
    except Exception:
        return False


def make_route_wrapper(route_func, label: str = "Response"):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed_chat(update):
            return

        username = update.effective_user.username or "unknown_user"
        command_name = label.lower().replace(" ", "_")
        logging.info(
            "[TelegramBot] 📥 Received /%s command from @%s",
            command_name,
            username,
        )

        try:
            logging.info(
                "[TelegramBot] 🟡 Calling route function: %s",
                route_func.__name__,
            )
            result = await route_func()

            def serialize_safe(val):
                try:
                    return json.dumps(val, indent=2, default=str)
                except Exception:
                    return str(val)

            formatted = serialize_safe(result)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            message = (
                f"🛰️ <b>{label}</b>\n"
                f"<i>{timestamp}</i>\n\n"
                f"<pre>{formatted}</pre>"
            )

            if update.message:
                await update.message.reply_text(
                    message,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

            logging.info("[TelegramBot] ✅ Successfully replied with %s data", label)

        except Exception as e:
            logging.exception(
                "[TelegramBot] ❌ Exception while handling /%s: %s",
                command_name,
                e,
            )
            error_msg = f"❌ <b>{label} Failed</b>\n<pre>{str(e)}</pre>"
            if update.message:
                await update.message.reply_text(error_msg, parse_mode="HTML")

    return handler


def start_telegram_bot():
    telegram_state = app_state.setdefault("telegram", {})

    if not getattr(config, "TELEGRAM_BOT_TOKEN", None):
        logging.warning("[TelegramBot] 🚫 TELEGRAM_BOT_TOKEN not configured")
        return None

    async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed_chat(update):
            return

        username = update.effective_user.username or "unknown_user"
        logging.info("[TelegramBot] 🧪 /test from %s", username)

        if update.message:
            await update.message.reply_text("✅ Test received")

    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed_chat(update):
            return

        text = (
            "<b>📚 Available Commands</b>\n\n"
            "🔹 <b>General</b>\n"
            "/test — Check if the bot is working\n"
            "/help — Show this help message\n"
            "/go_fetch — A fun little game of fetch 🐶🎾\n\n"
            "🌐 <b>Public Commands</b>\n"
            "/uptime_health — Basic uptime monitor check\n\n"
            "🔧 <b>Admin Commands</b>\n"
            "/start_stream — Manually start the trade data stream\n"
            "/shutdown_stream — Manually stop the trade data stream\n"
            "/stream_status — Stream health and connection statistics\n"
            "/metrics — System resource usage report\n"
            "/healthz — Comprehensive service-level health check\n"
            "/force_alert — Manually trigger alert message\n"
            "/config_all — Show default, override, and effective config values\n\n"
            "🧪 <b>Dev Commands</b>\n"
            "/test_positions — View current Alpaca positions\n"
            "/test_alert — Trigger a test email alert\n"
            "/test_telegram — Trigger a test Telegram alert\n"
            "/diagnostics — Full bot diagnostics and heartbeat check\n"
            "/trades — View summary of completed trades\n"
        )

        username = update.effective_user.username or "unknown_user"
        logging.info("[TelegramBot] 📘 Help command called by @%s", username)

        if update.message:
            await update.message.reply_text(text, parse_mode="HTML")

    async def go_fetch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed_chat(update):
            return

        username = update.effective_user.username or "unknown_user"
        logging.info("[TelegramBot] 🐶 /go_fetch from %s", username)

        if not update.message:
            return

        await update.message.reply_text("👧 winds up and throws the ball... 🎾")
        await asyncio.sleep(1.5)
        await update.message.reply_text("🐕 bolts off like lightning! 🏃‍♂️💨")
        await asyncio.sleep(1.5)
        await update.message.reply_text("🐕 leaps and catches it mid-air! 🐾✨")
        await asyncio.sleep(1.2)
        await update.message.reply_text("🐕 trots back proudly and drops it at her feet.")
        await asyncio.sleep(1)
        await update.message.reply_text("🐶 \"Woof! Again?\" 🐾🎾❤️")

    async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logging.info("[TelegramBot] 🧾 Raw update: %s", update.to_dict())

    async def shutdown_telegram_app(bot_app, reason: str = ""):
        if reason:
            logging.warning("[TelegramBot] 🛑 Shutting down Telegram bot. Reason: %s", reason)

        try:
            if getattr(bot_app, "updater", None) and bot_app.updater.running:
                await bot_app.updater.stop()
        except Exception as e:
            logging.debug("[TelegramBot] updater.stop() issue during shutdown: %s", e)

        try:
            if bot_app.running:
                await bot_app.stop()
        except Exception as e:
            logging.debug("[TelegramBot] bot_app.stop() issue during shutdown: %s", e)

        try:
            await bot_app.shutdown()
        except Exception as e:
            logging.debug("[TelegramBot] bot_app.shutdown() issue during shutdown: %s", e)

    async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        error = context.error

        if isinstance(error, Conflict):
            logging.warning("[TelegramBot] ⚠️ Telegram polling conflict detected while running.")
            logging.warning("[TelegramBot] ⚠️ Another instance is using this bot token.")
            logging.warning("[TelegramBot] 🛑 Stopping Telegram polling in this instance to avoid log spam.")

            bot_app = context.application
            if bot_app:
                await shutdown_telegram_app(
                    bot_app,
                    reason="telegram polling conflict",
                )
            return

        logging.exception("[TelegramBot] Unhandled Telegram polling error: %s", error)

    async def run_telegram_bot():
        bot_app = None

        async with telegram_start_lock:
            if telegram_state.get("bot_started"):
                logging.warning("[TelegramBot] 🚫 Bot already running inside lock — skipping start")
                return

            if is_shutdown_in_progress():
                logging.info("[TelegramBot] Shutdown already in progress. Skipping Telegram startup.")
                return

            telegram_state["bot_started"] = True

            try:
                allowed_ids = get_allowed_chat_ids()
                logging.info("[TelegramBot] ✅ Loaded Telegram chat IDs: %s", allowed_ids)

                if not allowed_ids:
                    logging.warning("[TelegramBot] ⚠️ No allowed Telegram chat IDs configured")

                bot_app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
                telegram_state["bot_app"] = bot_app
                bot_app.add_error_handler(telegram_error_handler)

                bot_app.add_handler(CommandHandler("test", test_command))
                bot_app.add_handler(CommandHandler("help", help_command))
                bot_app.add_handler(CommandHandler("go_fetch", go_fetch_command))

                bot_app.add_handler(
                    CommandHandler(
                        "uptime_health",
                        make_route_wrapper(uptime_health_check, "Uptime Health"),
                    )
                )

                bot_app.add_handler(CommandHandler("start_stream", make_route_wrapper(start_stream, "Start Stream")))
                bot_app.add_handler(CommandHandler("shutdown_stream", make_route_wrapper(shutdown_stream, "Shutdown Stream")))
                bot_app.add_handler(CommandHandler("stream_status", make_route_wrapper(stream_status, "Stream Status")))
                bot_app.add_handler(CommandHandler("metrics", make_route_wrapper(metrics, "Metrics")))
                bot_app.add_handler(CommandHandler("healthz", make_route_wrapper(health_check, "Full Health Check")))
                bot_app.add_handler(CommandHandler("force_alert", make_route_wrapper(force_alert, "Force Alert")))
                bot_app.add_handler(CommandHandler("config_all", make_route_wrapper(get_all_config, "Config Snapshot")))

                bot_app.add_handler(CommandHandler("test_positions", make_route_wrapper(test_positions, "Positions")))
                bot_app.add_handler(CommandHandler("test_alert", make_route_wrapper(test_alert, "Email Alert")))
                bot_app.add_handler(CommandHandler("test_telegram", make_route_wrapper(test_telegram_alert, "Telegram Alert")))
                bot_app.add_handler(CommandHandler("diagnostics", make_route_wrapper(run_diagnostics, "Diagnostics")))
                bot_app.add_handler(CommandHandler("trades", make_route_wrapper(view_trade_log, "Trades View")))

                bot_app.add_handler(MessageHandler(filters.ALL, log_all_updates))

                if is_shutdown_in_progress():
                    logging.info("[TelegramBot] Shutdown already in progress before startup delay. Aborting Telegram startup.")
                    return

                logging.info("[TelegramBot] ⏳ Waiting 20s before starting polling to let older instance shut down...")
                await asyncio.sleep(20)

                if is_shutdown_in_progress():
                    logging.info("[TelegramBot] Shutdown began during Telegram startup delay. Aborting Telegram startup.")
                    return

                logging.info("[TelegramBot] 🔄 Initializing Telegram bot...")
                if is_shutdown_in_progress():
                    logging.info("[TelegramBot] Shutdown already in progress before initialize(). Aborting Telegram startup.")
                    return
                await bot_app.initialize()

                logging.info("[TelegramBot] 🔄 Starting Telegram bot...")
                if is_shutdown_in_progress():
                    logging.info("[TelegramBot] Shutdown already in progress before start(). Aborting Telegram startup.")
                    return
                await bot_app.start()

                logging.info("[TelegramBot] 🔄 Starting Telegram polling...")
                if is_shutdown_in_progress():
                    logging.info("[TelegramBot] Shutdown already in progress before start_polling(). Aborting Telegram startup.")
                    await shutdown_telegram_app(bot_app, reason="shutdown before polling start")
                    return

                try:
                    await bot_app.updater.start_polling(drop_pending_updates=True)
                except Conflict:
                    logging.warning("[TelegramBot] ⚠️ Polling conflict detected during startup.")
                    logging.warning("[TelegramBot] ⏳ Backing off 60s, then shutting this instance down.")
                    await asyncio.sleep(60)
                    await shutdown_telegram_app(bot_app, reason="startup polling conflict")
                    return

                if is_shutdown_in_progress():
                    logging.info("[TelegramBot] Shutdown detected after polling start. Stopping Telegram bot.")
                    await shutdown_telegram_app(bot_app, reason="shutdown detected after polling start")
                    return

                for chat_id in allowed_ids:
                    if is_shutdown_in_progress():
                        logging.info("[TelegramBot] Shutdown detected before sending startup messages. Stopping Telegram bot.")
                        await shutdown_telegram_app(bot_app, reason="shutdown before startup messages")
                        return

                    try:
                        await bot_app.bot.send_message(
                            chat_id=chat_id,
                            text="🤖 Bot started and ready for /test",
                        )
                        logging.info("[TelegramBot] ✅ Startup message sent to %s", chat_id)
                    except Exception as e:
                        logging.warning(
                            "[TelegramBot] Couldn't send startup message to %s: %s",
                            chat_id,
                            e,
                        )

                logging.info("[TelegramBot] ✅ Telegram bot polling is active")

                while True:
                    await asyncio.sleep(5)

                    if is_shutdown_in_progress():
                        logging.info("[TelegramBot] Shutdown detected in polling loop. Exiting Telegram bot task.")
                        break

                    if bot_app.updater and not bot_app.updater.running:
                        logging.warning("[TelegramBot] Polling no longer running. Exiting Telegram bot task.")
                        break

            except asyncio.CancelledError:
                logging.info("[TelegramBot] 🛑 Telegram task cancelled.")
                raise

            except Exception:
                logging.exception("[TelegramBot] ❌ Telegram bot failed to start/run:")

            finally:
                if bot_app:
                    await shutdown_telegram_app(bot_app, reason="final cleanup")

                telegram_state["bot_started"] = False
                telegram_state["bot_app"] = None
                telegram_state["task"] = None
                logging.info("[TelegramBot] ✅ Telegram bot cleanup complete.")

    existing_task = telegram_state.get("task")
    if existing_task and not existing_task.done():
        logging.warning("[TelegramBot] 🚫 Existing Telegram task still running — skipping restart")
        return existing_task

    if is_shutdown_in_progress():
        logging.info("[TelegramBot] Shutdown already in progress. Refusing to create Telegram start task.")
        return None

    telegram_task = asyncio.create_task(
        run_telegram_bot(),
        name="telegram-bot-task",
    )
    telegram_state["task"] = telegram_task

    logging.info("[TelegramBot] ✅ Telegram start task created")
    return telegram_task