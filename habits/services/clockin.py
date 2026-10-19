from django.conf import settings
from django.contrib.auth.models import User
from django.core import signing
from django.urls import reverse

from ..models import get_logical_date

CLOCK_IN_SALT = "habits.clock_in"


def clock_in_url(user) -> str:
    # A personal link for today's reminders: it can only clock in this user's
    # habits for this logical day, so no login is needed to use it.
    token = signing.dumps(
        {"u": user.id, "d": get_logical_date().isoformat()}, salt=CLOCK_IN_SALT
    )
    return settings.SITE_URL.rstrip("/") + reverse("clock_in", args=[token])


def user_for_clock_in(token: str):
    """The user a clock-in link belongs to, or None if it's forged or not today's."""
    try:
        data = signing.loads(token, salt=CLOCK_IN_SALT)
    except signing.BadSignature:
        return None
    if data.get("d") != get_logical_date().isoformat():
        return None
    return User.objects.filter(id=data.get("u")).first()
