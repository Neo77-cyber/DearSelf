import datetime

from django.conf import settings
from django.utils import timezone

from ..models import ServiceFlag

WHATSAPP_FULL = "whatsapp_new_chats_full"


def whatsapp_signups_open() -> bool:
    if not settings.WHATSAPP_SIGNUPS_ENABLED:
        return False
    return not ServiceFlag.objects.filter(
        name=WHATSAPP_FULL, active_until__gt=timezone.now()
    ).exists()


def mark_whatsapp_full():
    # Green API's free plan allows a few chats per month. Close WhatsApp signups
    # until the 1st of next month; delete the flag in /admin/ to reopen sooner.
    today = timezone.localdate()
    first_of_next = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    until = timezone.make_aware(datetime.datetime.combine(first_of_next, datetime.time()))
    ServiceFlag.objects.update_or_create(
        name=WHATSAPP_FULL, defaults={"active_until": until}
    )
