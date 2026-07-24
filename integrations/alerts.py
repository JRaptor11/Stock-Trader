# integrations/alerts.py

import smtplib
import requests
import socket
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
import logging
from datetime import datetime, timedelta
from core.state import app_state
from config import runtime_config as config


TELEGRAM_CONNECT_TIMEOUT_SECONDS = 4.0
TELEGRAM_READ_TIMEOUT_SECONDS = 6.0
SMTP_TIMEOUT_SECONDS = 8.0


def _get_alerts_state() -> dict:
    utils_state = app_state.setdefault("utils", {})

    alerts_state = utils_state.get("alerts_utils")
    legacy_state = utils_state.get("alerts")

    if isinstance(alerts_state, dict):
        state = alerts_state
    elif isinstance(legacy_state, dict):
        state = legacy_state
    else:
        state = {}

    state.setdefault("email_suppressed", False)
    state.setdefault("email_suppression_reset", None)
    state.setdefault("use_telegram", True)

    # Keep both names pointed at the same dictionary for compatibility.
    utils_state["alerts_utils"] = state
    utils_state["alerts"] = state

    return state


def send_telegram_alert(message: str) -> bool:
    bot_token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
    configured_chat_ids = getattr(config, "TELEGRAM_CHAT_ID", None)

    if not bot_token or not configured_chat_ids:
        logging.warning("Telegram config not set. Skipping alert.")
        return False

    if isinstance(configured_chat_ids, (str, int)):
        chat_ids = [configured_chat_ids]
    else:
        try:
            chat_ids = list(configured_chat_ids)
        except TypeError:
            chat_ids = [configured_chat_ids]

    chat_ids = [
        chat_id
        for chat_id in chat_ids
        if str(chat_id).strip()
    ]

    if not chat_ids:
        logging.warning("No valid Telegram chat IDs configured.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    all_sent = True

    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": message,
        }

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=(
                    TELEGRAM_CONNECT_TIMEOUT_SECONDS,
                    TELEGRAM_READ_TIMEOUT_SECONDS,
                ),
            )

            if response.status_code == 200:
                logging.info(
                    "📩 Telegram alert sent | chat_id=%s",
                    chat_id,
                )
            else:
                all_sent = False
                logging.error(
                    "❌ Telegram alert failed | chat_id=%s status=%s response=%s",
                    chat_id,
                    response.status_code,
                    response.text,
                )

        except requests.Timeout:
            all_sent = False
            logging.error(
                "❌ Telegram alert timed out | chat_id=%s "
                "connect_timeout=%.1fs read_timeout=%.1fs",
                chat_id,
                TELEGRAM_CONNECT_TIMEOUT_SECONDS,
                TELEGRAM_READ_TIMEOUT_SECONDS,
            )

        except requests.RequestException as exc:
            all_sent = False
            logging.error(
                "❌ Telegram network failure | chat_id=%s error=%s",
                chat_id,
                exc,
            )

        except Exception:
            all_sent = False
            logging.exception(
                "❌ Unexpected Telegram alert failure | chat_id=%s",
                chat_id,
            )

    return all_sent


def send_email_alert(subject: str, body: str) -> bool:
    alerts_state = _get_alerts_state()

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    full_body = f"[Sent at {timestamp}]\n\n{body}"

    email_address = getattr(config, "EMAIL_ADDRESS", None)
    email_password = getattr(config, "EMAIL_PASSWORD", None)
    configured_recipients = getattr(config, "EMAIL_RECIPIENTS", None) or []

    if isinstance(configured_recipients, str):
        recipients = [
            recipient.strip()
            for recipient in configured_recipients.split(",")
            if recipient.strip()
        ]
    else:
        recipients = [
            str(recipient).strip()
            for recipient in configured_recipients
            if str(recipient).strip()
        ]

    email_sent = False

    try:
        if alerts_state.get("email_suppressed"):
            logging.warning(
                "📭 Email suppressed due to previous Gmail quota error."
            )

        elif not email_address or not email_password or not recipients:
            logging.warning(
                "Email configuration is incomplete. Skipping email alert."
            )

        else:
            logging.info("Attempting to send alert: %s", subject)

            message = MIMEText(full_body)
            message["Subject"] = subject
            message["From"] = email_address
            message["To"] = ", ".join(recipients)

            with smtplib.SMTP_SSL(
                "smtp.gmail.com",
                465,
                timeout=SMTP_TIMEOUT_SECONDS,
            ) as smtp:
                if smtp.sock is not None:
                    smtp.sock.settimeout(SMTP_TIMEOUT_SECONDS)

                smtp.login(
                    email_address,
                    email_password,
                )
                smtp.send_message(message)

            email_sent = True
            logging.info("✅ Email/SMS sent: %s", subject)

    except smtplib.SMTPDataError as exc:
        quota_exceeded = (
            exc.smtp_code == 550
            and b"daily user sending limit exceeded"
            in exc.smtp_error.lower()
        )

        if quota_exceeded:
            logging.error(
                "🚫 Gmail daily limit reached. Suppressing further "
                "email alerts until midnight UTC."
            )
            alerts_state["email_suppressed"] = True
            alerts_state["email_suppression_reset"] = (
                datetime.utcnow().date() + timedelta(days=1)
            )
        else:
            logging.error(
                "❌ Email SMTP data failure | subject=%s error=%s",
                subject,
                exc,
            )

    except (socket.timeout, TimeoutError):
        logging.error(
            "❌ Email alert timed out after %.1f seconds | subject=%s",
            SMTP_TIMEOUT_SECONDS,
            subject,
        )

    except smtplib.SMTPException as exc:
        logging.error(
            "❌ Email SMTP failure | subject=%s error=%s",
            subject,
            exc,
        )

    except OSError as exc:
        logging.error(
            "❌ Email network failure | subject=%s error=%s",
            subject,
            exc,
        )

    except Exception:
        logging.exception(
            "❌ Unexpected email alert failure | subject=%s",
            subject,
        )

    # Telegram is independent of whether email succeeded.
    if alerts_state.get("use_telegram", False):
        try:
            send_telegram_alert(f"{subject}\n\n{full_body}")
        except Exception:
            logging.exception(
                "❌ Unexpected failure invoking Telegram fallback."
            )

    return email_sent