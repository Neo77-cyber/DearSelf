import logging

from .clockin import clock_in_url
from .telegram import send_message
from .whatsapp import send_whatsapp_message

logger = logging.getLogger(__name__)


def habit_lines(habits) -> str:
    return "\n".join(
        f"• {h.name}: {h.current_streak} day streak | {h.missed_count}/3 misses"
        for h in habits
    )


def done_buttons(habits):
    def label(h):
        return f"✅ Stayed off: {h.name}" if h.is_quit_habit else f"✅ Did it: {h.name}"

    return [[(label(h), f"done:{h.id}")] for h in habits if not h.marked_today]


def send_text(user, text: str) -> bool:
    """Send on whichever channel the user signed up with."""
    profile = user.profile
    if profile.telegram_chat_id:
        # *bold* is WhatsApp formatting; Telegram plain text would show the stars.
        return send_message(profile.telegram_chat_id, text.replace("*", ""))
    if profile.phone_number:
        return send_whatsapp_message(profile.phone_number, text)
    logger.warning("User %s has no channel to message.", user.username)
    return False


def send_reminder(user, title: str, habits, note: str) -> bool:
    body = f"{title}\n\n{habit_lines(habits)}\n\n{note}"
    profile = user.profile
    if profile.telegram_chat_id:
        # One tap inside Telegram, no link needed.
        return send_message(
            profile.telegram_chat_id, body.replace("*", ""), done_buttons(habits)
        )
    return send_text(user, f"{body}\n\nTap to clock in:\n{clock_in_url(user)}")
