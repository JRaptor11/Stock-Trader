import smtplib
import requests
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
import logging
from datetime import datetime, timedelta
from state import app_state
import utils.config_utils as config

def send_telegram_alert(message: str):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logging.warning("Telegram config not set. Skipping alert.")
        return

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            logging.info("📩 Telegram alert sent.")
        else:
            logging.error(f"❌ Telegram alert failed: {response.text}")
    except Exception as e:
        logging.error(f"❌ Exception sending Telegram alert: {e}")


def send_email_alert(subject, body):
    try:
        if app_state["utils"]["alerts_utils"].get("email_suppressed"):
            logging.warning("📭 Email suppressed due to previous Gmail quota error.")
            return

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        full_body = f"[Sent at {timestamp}]\n\n{body}"

        logging.info(f"Attempting to send alert: {subject}")
        msg = MIMEText(full_body)
        msg["Subject"] = subject
        msg["From"] = config.EMAIL_ADDRESS
        msg["To"] = ", ".join(config.EMAIL_RECIPIENTS)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(config.EMAIL_ADDRESS, config.EMAIL_PASSWORD)
            smtp.send_message(msg)

        logging.info(f"✅ Email/SMS sent: {subject}")

        # ✅ Send Telegram if toggle is enabled
        if app_state["utils"]["alerts_utils"].get("use_telegram", False):
             send_telegram_alert(f"{subject}\n\n{full_body}")
    
    except smtplib.SMTPDataError as e:
        if e.smtp_code == 550 and b'daily user sending limit exceeded' in e.smtp_error.lower():
            logging.error("🚫 Gmail daily limit reached. Suppressing further email alerts until midnight UTC.")
            app_state["utils"]["alerts_utils"]["email_suppressed"] = True
            app_state["utils"]["alerts_utils"]["email_suppression_reset"] = datetime.utcnow().date() + timedelta(days=1)
        else:
            logging.error(f"❌ Failed to send email (SMTPDataError): {e}")
    except Exception as e:
        logging.error(f"❌ Failed to send email: {e}")
