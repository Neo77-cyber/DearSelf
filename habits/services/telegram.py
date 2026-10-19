import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def telegram_enabled() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_BOT_USERNAME)


def bot_link(start_payload: str = "") -> str:
    link = f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}"
    return f"{link}?start={start_payload}" if start_payload else link


def call(method: str, _timeout: int = 10, **payload):
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram call %s skipped: no bot token.", method)
        return None
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}",
            json=payload,
            timeout=_timeout,
        )
        data = response.json()
    except Exception as e:
        logger.error("Telegram %s failed: %s", method, e)
        return None
    if not data.get("ok"):
        logger.error("Telegram %s error: %s", method, data.get("description"))
        return None
    return data.get("result")


def keyboard(rows):
    """rows: [[(label, callback_data), ...], ...]"""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def send_message(chat_id, text, rows=None) -> bool:
    payload = {
        "chat_id": chat_id,
        "text": text,
        # Previews would make Telegram open our links (e.g. one-time login links).
        "link_preview_options": {"is_disabled": True},
    }
    if rows:
        payload["reply_markup"] = keyboard(rows)
    return call("sendMessage", **payload) is not None


def edit_buttons(chat_id, message_id, rows=None):
    call(
        "editMessageReplyMarkup",
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=keyboard(rows or []),
    )


def answer_callback(callback_id, text=""):
    call("answerCallbackQuery", callback_query_id=callback_id, text=text)
