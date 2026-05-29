import hashlib
import secrets
import logging
from .whatsapp import send_otp_whatsapp, is_whatsapp_quota_exceeded
from django.utils import timezone

logger = logging.getLogger(__name__)

OTP_EXPIRY_MINUTES = 15
OTP_MAX_ATTEMPTS = 5


def generate_otp() -> str:
    return str(secrets.randbelow(900000) + 100000)


def send_otp(phone_number: str, otp: str) -> tuple[bool, str]:
    if not is_whatsapp_quota_exceeded():
        sent = send_otp_whatsapp(phone_number, otp, OTP_EXPIRY_MINUTES)
        if sent:
            return True, "whatsapp"
    return False, None


def store_otp(request, phone_number: str, otp: str):
    hashed = hashlib.sha256(otp.encode()).hexdigest()
    expires_at = (
        timezone.now() + timezone.timedelta(minutes=OTP_EXPIRY_MINUTES)
    ).timestamp()
    request.session["otp_data"] = {
        "otp_hash": hashed,
        "phone": phone_number,
        "expires_at": expires_at,
        "attempts": 0,
    }
    request.session.modified = True
    request.session.save()


def verify_otp(request, phone_number: str, submitted_otp: str) -> tuple[bool, str]:
    data = request.session.get("otp_data")
    if not data:
        return False, "Session expired. Please register again."

    if data.get("phone") != phone_number:
        return False, "Phone number mismatch. Please start again."

    if timezone.now().timestamp() > data["expires_at"]:
        clear_otp(request)
        return False, "OTP has expired. Please request a new one."

    if data.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        clear_otp(request)
        return False, "Too many failed attempts. Please start again."

    submitted_hash = hashlib.sha256(submitted_otp.strip().encode()).hexdigest()
    if data["otp_hash"] != submitted_hash:
        data["attempts"] = data.get("attempts", 0) + 1
        request.session["otp_data"] = data
        request.session.modified = True
        remaining = OTP_MAX_ATTEMPTS - data["attempts"]
        return False, f"Wrong code. {remaining} attempt(s) left."

    clear_otp(request)
    return True, "ok"


def clear_otp(request):
    request.session.pop("otp_data", None)
    request.session.modified = True