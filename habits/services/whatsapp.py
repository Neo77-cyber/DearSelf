import requests
from whatsapp_api_client_python import API
from django.conf import settings
from django.core.cache import cache
import logging
from .helpers import mask_phone

logger = logging.getLogger(__name__)

WA_QUOTA_EXCEEDED_KEY = "green_api_quota_exceeded"
WA_QUOTA_COOLDOWN = 60 * 60 * 6


def is_whatsapp_quota_exceeded() -> bool:
    return cache.get(WA_QUOTA_EXCEEDED_KEY) is not None


def mark_whatsapp_quota_exceeded():
    cache.set(WA_QUOTA_EXCEEDED_KEY, True, timeout=WA_QUOTA_COOLDOWN)
    logger.warning(
        "Green API quota exhausted — falling back to email for %dh.",
        WA_QUOTA_COOLDOWN // 3600,
    )


def send_whatsapp_message(phone_number: str, message: str) -> bool:
    if settings.WHATSAPP_DRY_RUN:
        # Local testing: log instead of sending, so no Green API chat slots are used.
        logger.warning("[WHATSAPP DRY RUN] to %s:\n%s", phone_number, message)
        return True

    if is_whatsapp_quota_exceeded():
        logger.info("WhatsApp quota cooldown active — skipping send.")
        return False

    chat_id = f"{phone_number}@c.us"

    try:
        url = (
            f"{settings.GREEN_API_URL.rstrip('/')}/waInstance{settings.GREEN_API_ID}/"
            f"sendMessage/{settings.GREEN_API_TOKEN}"
        )
        payload = {
            "chatId": chat_id,
            "message": message,
            "linkPreview": True,
        }

        response = requests.post(url, json=payload, timeout=10)
        data = response.json()

        if response.status_code == 200:
            return True

        if response.status_code == 466:
            # Chat limit for the plan: existing chats still work, new numbers don't.
            from .flags import mark_whatsapp_full

            mark_whatsapp_full()
            logger.error("Green API chat quota reached: %s", data)
            return False

        if response.status_code in (429, 403):
            mark_whatsapp_quota_exceeded()
            logger.error("Green API quota error %s: %s", response.status_code, data)
            return False

        logger.error("Green API error %s: %s", response.status_code, data)
        return False

    except requests.exceptions.Timeout:
        logger.error("WhatsApp send timed out for %s", mask_phone(phone_number))
        return False
    except Exception as e:
        error_str = str(e).lower()
        if "quota" in error_str or "limit" in error_str or "exhausted" in error_str:
            mark_whatsapp_quota_exceeded()
        logger.error("Failed to send WA message to %s: %s", mask_phone(phone_number), e)
        return False


def send_otp_whatsapp(phone_number: str, otp: str, expiry_minutes: int) -> bool:
    if is_whatsapp_quota_exceeded():
        return False
    message = (
        f"*Your DearSelf verification code is: {otp}*\n\n"
        f"It expires in {expiry_minutes} minutes. Do not share this with anyone."
    )
    return send_whatsapp_message(phone_number, message)
