import logging

from django.db import IntegrityError

from ..models import Habit, Profile
from .accounts import create_account, login_link
from .notify import done_buttons, habit_lines
from .telegram import answer_callback, edit_buttons, send_message

logger = logging.getLogger(__name__)

# Telegram /start payloads only allow [A-Za-z0-9_-], so "WORK OUT" -> "WORK_OUT".
PICKABLE_HABITS = {
    key.replace(" ", "_"): (key, label)
    for key, label in Habit.HABIT_CHOICES
    if key != "CUSTOM"
}

WELCOME = (
    "Welcome to DearSelf. I'll remind you at 8am, and again at 9pm if you "
    "haven't clocked in; tap ✅ to clock in. Miss a habit 3 days in a row and "
    "that habit is gone for good. Lose all your habits and you're out. Be warned."
)
OUT = "You've lost all your habits, so you're out. DearSelf is closed for you."
HELP = (
    "/done — clock in everything for today\n"
    "/status — your streaks\n"
    "/dashboard — a one-time link to the website"
)


def user_for_chat(chat_id):
    profile = (
        Profile.objects.filter(telegram_chat_id=chat_id).select_related("user").first()
    )
    return profile.user if profile else None


def handle_update(update: dict):
    if "callback_query" in update:
        return handle_button(update["callback_query"])

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id = chat["id"]
    text = (message.get("text") or "").strip()
    command, _, arg = text.partition(" ")
    command = command.split("@")[0].lower()

    if command == "/start":
        return start(chat_id, arg.strip())

    user = user_for_chat(chat_id)
    if user is None:
        return ask_for_habit(chat_id)
    if not user.is_active:
        return send_message(chat_id, OUT)

    if command == "/done":
        return clock_in_all(chat_id, user)
    if command == "/status":
        return send_status(chat_id, user)
    if command == "/dashboard":
        return send_message(
            chat_id,
            "Your login link (works once, for 15 minutes):\n" + login_link(user),
        )
    return send_message(chat_id, HELP)


def start(chat_id, payload):
    user = user_for_chat(chat_id)
    if user is not None:
        if not user.is_active:
            return send_message(chat_id, OUT)
        return send_status(chat_id, user, intro="You're already in.")
    if payload in PICKABLE_HABITS:
        return sign_up(chat_id, payload)
    return ask_for_habit(chat_id)


def ask_for_habit(chat_id):
    buttons = [(label, f"pick:{slug}") for slug, (_, label) in PICKABLE_HABITS.items()]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    send_message(chat_id, "Which habit are you building?", rows)


def sign_up(chat_id, slug):
    key, label = PICKABLE_HABITS[slug]
    try:
        user = create_account(habit_name=label, category=key, telegram_chat_id=chat_id)
    except IntegrityError:
        # Double tap on Start: the first tap already created the account.
        user = user_for_chat(chat_id)
        if user is None:
            raise
        return send_status(chat_id, user, intro="You're already in.")
    send_message(chat_id, WELCOME)
    send_status(chat_id, user, intro="Done it today already? Tap below.")


def send_status(chat_id, user, intro=""):
    habits = list(Habit.objects.filter(user=user).order_by("id"))
    text = f"{intro}\n\n{habit_lines(habits)}".strip()
    live = [h for h in habits if not h.is_lost]
    if live and all(h.marked_today for h in live):
        text += "\n\nAll done today. 🔥"
    send_message(chat_id, text, done_buttons(habits))


def clock_in_all(chat_id, user):
    lines = []
    for habit in Habit.objects.filter(user=user).order_by("id"):
        if not habit.marked_today and not habit.is_lost:
            result = habit.mark_done()
            lines.append(f"🔥 {habit.name}: {result['current_streak']} day streak")
    send_message(chat_id, "\n".join(lines) or "You already clocked in today. 🔥")


def handle_button(callback):
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return answer_callback(callback["id"])

    action, _, value = data.partition(":")
    user = user_for_chat(chat_id)

    if action == "pick":
        answer_callback(callback["id"])
        edit_buttons(chat_id, message.get("message_id"))
        if user is not None:
            return send_status(chat_id, user, intro="You're already in.")
        if value in PICKABLE_HABITS:
            return sign_up(chat_id, value)
        return ask_for_habit(chat_id)

    if action == "done":
        if user is None or not user.is_active:
            answer_callback(callback["id"])
            return send_message(chat_id, OUT if user else "Send /start to begin.")
        habit = Habit.objects.filter(id=value, user=user).first() if value.isdigit() else None
        if habit is None:
            return answer_callback(callback["id"], "That habit wasn't found.")
        if habit.is_lost:
            answer_callback(callback["id"], "That streak is lost. The habit is done.")
        elif habit.marked_today:
            answer_callback(callback["id"], "Already clocked in today.")
        else:
            result = habit.mark_done()
            answer_callback(callback["id"], f"🔥 {result['current_streak']} day streak")
            send_message(
                chat_id, f"🔥 {habit.name}: {result['current_streak']} day streak. Keep going."
            )
        # Refresh the buttons so finished habits disappear.
        habits = list(Habit.objects.filter(user=user).order_by("id"))
        return edit_buttons(chat_id, message.get("message_id"), done_buttons(habits))

    answer_callback(callback["id"])
