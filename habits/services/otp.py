import hashlib
import hmac
import secrets
import logging
from django.conf import settings
from django.db import IntegrityError
from django.db.models import F
from django.utils import timezone
from .whatsapp import send_otp_whatsapp, is_whatsapp_quota_exceeded
from ..models import PhoneOTP

logger = logging.getLogger(__name__)

OTP_EXPIRY_MINUTES = 15
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 30


def generate_otp() -> str:
    return str(secrets.randbelow(900000) + 100000)


def hash_otp(otp: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(), otp.strip().encode(), hashlib.sha256
    ).hexdigest()


def send_otp(phone_number: str, otp: str) -> tuple[bool, str]:
    if not is_whatsapp_quota_exceeded():
        sent = send_otp_whatsapp(phone_number, otp, OTP_EXPIRY_MINUTES)
        if sent:
            return True, "whatsapp"
    return False, None


def claim_otp_send(phone_number: str) -> bool:
    """Atomically reserve the right to send a code. Of several concurrent
    requests for the same phone (a double tap), exactly one gets True."""
    now = timezone.now()
    try:
        _, created = PhoneOTP.objects.get_or_create(
            phone_number=phone_number,
            defaults={"expires_at": now, "sent_at": now},
        )
    except IntegrityError:
        created = False
    if created:
        return True
    cutoff = now - timezone.timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS)
    return bool(
        PhoneOTP.objects.filter(phone_number=phone_number, sent_at__lt=cutoff).update(
            sent_at=now
        )
    )


def release_otp_send(phone_number: str):
    """Give the slot back after a failed send so the user can retry at once."""
    PhoneOTP.objects.filter(phone_number=phone_number).update(
        sent_at=timezone.now()
        - timezone.timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS + 1)
    )


def issue_otp(phone_number: str) -> str:
    """Send a fresh code unless one just went out. Returns "sent",
    "cooldown" (a double tap: reuse the code already sent) or "failed"."""
    if not claim_otp_send(phone_number):
        return "cooldown"
    otp = generate_otp()
    sent, _ = send_otp(phone_number, otp)
    if not sent:
        release_otp_send(phone_number)
        return "failed"
    store_otp(phone_number, otp)
    return "sent"


def store_otp(phone_number: str, otp: str):
    now = timezone.now()
    PhoneOTP.objects.update_or_create(
        phone_number=phone_number,
        defaults={
            "otp_hash": hash_otp(otp),
            "expires_at": now + timezone.timedelta(minutes=OTP_EXPIRY_MINUTES),
            "attempts": 0,
            "sent_at": now,
        },
    )


def verify_otp(phone_number: str, submitted_otp: str) -> tuple[bool, str]:
    record = PhoneOTP.objects.filter(phone_number=phone_number).first()
    if not record or not record.otp_hash:
        return False, "Your code has expired. Please request a new one."

    if timezone.now() > record.expires_at:
        clear_otp(phone_number)
        return False, "OTP has expired. Please request a new one."

    if record.attempts >= OTP_MAX_ATTEMPTS:
        return False, "Too many failed attempts. Please request a new code."

    if not hmac.compare_digest(record.otp_hash, hash_otp(submitted_otp)):
        PhoneOTP.objects.filter(pk=record.pk).update(attempts=F("attempts") + 1)
        remaining = max(OTP_MAX_ATTEMPTS - record.attempts - 1, 0)
        return False, f"Wrong code. {remaining} attempt(s) left."

    # Consume the code atomically so it can only be used once.
    deleted, _ = PhoneOTP.objects.filter(
        pk=record.pk, otp_hash=record.otp_hash
    ).delete()
    if not deleted:
        return False, "This code was already used. Please log in."
    return True, "ok"


def clear_otp(phone_number: str):
    PhoneOTP.objects.filter(phone_number=phone_number).delete()
