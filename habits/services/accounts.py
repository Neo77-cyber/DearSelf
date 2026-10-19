from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core import signing
from django.db import transaction
from django.urls import reverse

from ..models import Habit, Profile

LOGIN_LINK_SALT = "habits.login_link"
LOGIN_LINK_MAX_AGE = 15 * 60


def create_account(*, habit_name, category, phone=None, telegram_chat_id=None):
    """One account per phone (WhatsApp signup) or chat id (Telegram signup).
    Nobody sets a password: WhatsApp users log in with a code, Telegram users
    with a link from the bot."""
    username = phone or f"tg{telegram_chat_id}"
    with transaction.atomic():
        user = User.objects.create(username=username, password=make_password(None))
        Profile.objects.create(
            user=user,
            phone_number=phone,
            telegram_chat_id=telegram_chat_id,
            is_whatsapp_verified=bool(phone),
        )
        Habit.objects.create(user=user, name=habit_name, category=category)
    return user


def login_link(user) -> str:
    # last_login is part of the token, so a link stops working once it's used.
    token = signing.dumps(
        {"u": user.id, "l": user.last_login.isoformat() if user.last_login else ""},
        salt=LOGIN_LINK_SALT,
    )
    return settings.SITE_URL.rstrip("/") + reverse("login_link", args=[token])


def user_for_login_link(token: str):
    try:
        data = signing.loads(token, salt=LOGIN_LINK_SALT, max_age=LOGIN_LINK_MAX_AGE)
    except signing.BadSignature:
        return None
    user = User.objects.filter(id=data.get("u"), is_active=True).first()
    if user is None:
        return None
    current = user.last_login.isoformat() if user.last_login else ""
    return user if current == data.get("l") else None
