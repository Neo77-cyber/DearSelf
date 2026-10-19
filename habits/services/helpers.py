import ipaddress
from typing import Union
import phonenumbers
from ..models import Habit


def clean_phone_number(raw_phone: str) -> Union[str, None]:

    try:
        parsed = phonenumbers.parse(raw_phone, "NG")
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            ).replace("+", "")
    except phonenumbers.NumberParseException:
        pass
    return None


def parse_habit(post_data: dict) -> Union[tuple[str, str], tuple[None, None]]:
    choice = post_data.get("habit_choice", "").strip()
    custom_name = post_data.get("custom_habit", "").strip()

    if choice == "custom":
        if not custom_name:
            return None, None
        return custom_name[:100], "CUSTOM"

    habit_name = dict(Habit.HABIT_CHOICES).get(choice.upper())
    if not habit_name:
        return None, None
    return habit_name, choice.upper()


def banned_check(user) -> bool:

    return not user.is_active


def mask_phone(phone: str) -> str:
    if not phone or len(phone) < 7:
        return "***"
    return phone[:4] + "****" + phone[-3:]


def client_ip(request) -> str:
    # Production sits behind Cloudflare + Render, so REMOTE_ADDR is a proxy shared by
    # every visitor. Cloudflare overwrites CF-Connecting-IP, so clients can't spoof it.
    candidates = [
        request.META.get("HTTP_CF_CONNECTING_IP", ""),
        request.META.get("HTTP_TRUE_CLIENT_IP", ""),
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0],
        request.META.get("REMOTE_ADDR", ""),
    ]
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return candidate
    return ""
