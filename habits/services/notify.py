import logging

from .clockin import clock_in_url
from .telegram import send_message
from .whatsapp import send_whatsapp_message

logger = logging.getLogger(__name__)


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def habit_line(h) -> str:
    if h.marked_today:
        return f"✅ {h.name} 🔥 {h.current_streak}"
    if h.is_lost:
        return f"💀 {h.name}: streak lost. Clock in to start again."
    if h.missed_count:
        return f"⏳ {h.name}: missed {plural(h.missed_count, 'day')}, {plural(h.days_left, 'day')} left"
    if h.current_streak:
        return f"🔥 {h.name} {h.current_streak}"
    return f"🌱 {h.name}: day 1 starts today"


def habit_lines(habits) -> str:
    return "\n".join(habit_line(h) for h in habits)


def warning_lines(habits) -> str:
    """For the 9pm message: how close each unfinished habit is to being lost."""
    lines = []
    for h in habits:
        if h.marked_today or h.is_lost or not h.missed_count:
            continue
        if h.days_left == 1:
            lines.append(f"⚠️ {h.name}: miss today and the streak is lost.")
        else:
            lines.append(f"⚠️ {h.name}: {plural(h.days_left, 'day')} left before you lose the streak.")
    return "\n".join(lines)


def done_buttons(habits):
    def label(h):
        if h.is_custom_habit:
            return f"✅ Passed: {h.name}"
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
