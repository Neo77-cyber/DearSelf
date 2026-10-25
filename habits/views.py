import json
import logging
from collections import defaultdict
import hmac

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core import signing
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt


from django_ratelimit.decorators import ratelimit
from django_ratelimit.core import is_ratelimited


from .models import Habit, Profile
from .services.accounts import create_account, user_for_login_link
from .services.clockin import user_for_clock_in
from .services.flags import whatsapp_signups_open
from .services.streaks import close_day
from .services.helpers import clean_phone_number, parse_habit, banned_check
from .services.notify import send_reminder, send_text, warning_lines
from .services.otp import (
    OTP_EXPIRY_MINUTES,
    issue_otp,
    verify_otp as verify_otp_code,
)
from .services.telegram import bot_link, telegram_enabled
from .services.telegram_bot import handle_update
from .services.whatsapp import whatsapp_status

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sign up
# ---------------------------------------------------------------------------


def render_index(request, status=200):
    return render(
        request,
        "habits/index.html",
        {
            "whatsapp_open": whatsapp_signups_open(),
            "telegram_url": bot_link() if telegram_enabled() else "",
        },
        status=status,
    )


@ratelimit(key="ip", rate="10/m", method="POST", block=True)
def index(request):
    if request.user.is_authenticated:
        return redirect("habit_list")

    if request.method != "POST":
        return render_index(request)

    if not whatsapp_signups_open():
        messages.error(request, "WhatsApp signups are full right now. Start on Telegram instead.")
        return render_index(request)

    clean_number = clean_phone_number(request.POST.get("identifier", "").strip())
    if not clean_number:
        messages.error(
            request,
            "Phone number is invalid. Please remove the '0' before your number (e.g. 8123 not 08123).",
        )
        return render_index(request)

    if is_ratelimited(
        request,
        group="register_phone",
        key=lambda g, r: clean_number,
        rate="10/h",
        method="POST",
        increment=True,
    ):
        messages.error(
            request, "Too many attempts for this number. Please try again later."
        )
        return render_index(request)

    habit_name, category = parse_habit(request.POST)
    if not habit_name:
        messages.error(request, "Please select a valid habit.")
        return render_index(request)

    if User.objects.filter(username=clean_number).exists():
        messages.info(
            request, "An account with this number already exists. Please log in."
        )
        return redirect("login")

    request.session["pending_registration"] = {
        "phone": clean_number,
        "habit_name": habit_name,
        "category": category,
    }
    request.session.modified = True
    request.session.save()

    if issue_otp(clean_number) == "failed":
        if not whatsapp_signups_open():
            messages.error(
                request,
                "WhatsApp signups just filled up. Start on Telegram instead: it's one tap.",
            )
        else:
            messages.error(
                request, "We couldn't reach that number on WhatsApp. Please try again."
            )
        return render_index(request)

    return redirect("verify_otp")


# --- verify_otp_view ---

VERIFY_PHONE_SALT = "habits.verify_otp.phone"


def render_verify(request, pending, purpose="signup"):
    # The signed phone lets a duplicate submit (double tap, slow network) that
    # arrives after login has rotated the session recognise the finished signup.
    signed_phone = signing.dumps(pending["phone"], salt=VERIFY_PHONE_SALT)
    return render(
        request,
        "habits/verify_otp.html",
        {
            "signed_phone": signed_phone,
            "form_action": reverse("verify_otp" if purpose == "signup" else "login_verify"),
            "button_label": "Verify & Create Account" if purpose == "signup" else "Log In",
        },
    )


def already_verified_phone(request):
    try:
        phone = signing.loads(
            request.POST.get("signed_phone", ""),
            salt=VERIFY_PHONE_SALT,
            max_age=OTP_EXPIRY_MINUTES * 60 * 2,
        )
    except signing.BadSignature:
        return None
    if Profile.objects.filter(phone_number=phone, is_whatsapp_verified=True).exists():
        return phone
    return None


@ratelimit(key="ip", rate="5/m", method="POST", block=True)
def verify_otp_view(request):

    if request.user.is_authenticated:
        return redirect("habit_list")

    pending = request.session.get("pending_registration")
    if not pending and request.method == "POST" and already_verified_phone(request):
        messages.success(request, "Your account is ready. Log in to continue.")
        return redirect("login")

    if not pending:
        messages.error(request, "Session expired. Please register again.")
        return redirect("index")

    if request.method != "POST":
        return render_verify(request, pending)

    submitted_otp = request.POST.get("otp", "").strip()
    if not submitted_otp:
        messages.error(request, "Please enter the code we sent you.")
        return render_verify(request, pending)

    valid, msg = verify_otp_code(pending["phone"], submitted_otp)
    if not valid and already_verified_phone(request):
        # A concurrent duplicate submit already used the code and created the account.
        messages.success(request, "Your account is ready. Log in to continue.")
        return redirect("login")
    if not valid:
        messages.error(request, msg)
        return render_verify(request, pending)

    phone = pending["phone"]
    request.session.pop("pending_registration", None)
    request.session.save()

    if User.objects.filter(username=phone).exists():
        messages.info(
            request, "An account with this number already exists. Please log in."
        )
        return redirect("login")

    try:
        user = create_account(
            habit_name=pending["habit_name"], category=pending["category"], phone=phone
        )
    except IntegrityError:
        logger.warning(
            "IntegrityError on account creation for %s — possible race", phone
        )
        messages.info(
            request, "An account with this number already exists. Please log in."
        )
        return redirect("login")
    except Exception as exc:
        logger.error(
            "Account creation failed after OTP for %s: %s", phone, exc, exc_info=True
        )
        messages.error(request, "Something went wrong. Please try again.")
        return redirect("index")

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    try:
        send_text(
            user,
            "Welcome to DearSelf. I'll remind you at 8am, and again at 9pm if "
            "you haven't clocked in; tap the link to clock in. Miss a habit 3 "
            "days in a row and you lose its streak. Lose every habit and you "
            "are out. Be warned.",
        )
    except Exception as e:
        logger.warning("Welcome message failed for %s: %s", phone[:4], e)

    return redirect("habit_list")


# --- resend_otp ---


@ratelimit(key="ip", rate="10/m", method="POST", block=True)
def resend_otp(request):
    pending = request.session.get("pending_login") or request.session.get(
        "pending_registration"
    )
    if not pending:
        messages.error(request, "Session expired. Please start again.")
        return redirect("index")
    back = "login_verify" if request.session.get("pending_login") else "verify_otp"

    result = issue_otp(pending["phone"])
    if result == "cooldown":
        messages.info(
            request, "We just sent you a code. Give it a few seconds to arrive."
        )
    elif result == "failed":
        messages.error(request, "Failed to resend. Please try again.")
    else:
        messages.success(request, "A new code has been sent to your WhatsApp.")
    return redirect(back)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def render_login(request):
    return render(
        request,
        "habits/login.html",
        {"telegram_url": bot_link() if telegram_enabled() else ""},
    )


@ratelimit(key="ip", rate="10/m", method="POST", block=True)
def login_view(request):
    if request.user.is_authenticated:
        return redirect("habit_list")

    if request.method != "POST":
        return render_login(request)

    clean_number = clean_phone_number(request.POST.get("identifier", "").strip())
    password = request.POST.get("password", "")
    if not clean_number:
        messages.error(request, "That phone number is not valid.")
        return render_login(request)

    if is_ratelimited(
        request,
        group="login_user",
        key=lambda g, r: clean_number,
        rate="10/h",
        method="POST",
        increment=True,
    ):
        messages.error(
            request,
            "Too many login attempts for this number. Try again in an hour.",
        )
        return render_login(request)

    existing = User.objects.filter(username=clean_number).first()
    if existing is not None and not existing.is_active:
        return redirect("banned")

    if password:
        # Accounts created before codes replaced passwords can still use them.
        user = authenticate(request, username=clean_number, password=password)
        if user is None:
            messages.error(request, "Invalid number or password.")
            return render_login(request)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("habit_list")

    if existing is None:
        messages.error(request, "No account uses that number yet. Sign up first.")
        return render_login(request)

    request.session["pending_login"] = {"phone": clean_number}
    request.session.modified = True
    request.session.save()

    if issue_otp(clean_number) == "failed":
        messages.error(request, "We couldn't send your code. Please try again.")
        return render_login(request)
    return redirect("login_verify")


@ratelimit(key="ip", rate="5/m", method="POST", block=True)
def login_verify(request):
    if request.user.is_authenticated:
        return redirect("habit_list")

    pending = request.session.get("pending_login")
    if not pending:
        messages.error(request, "Session expired. Please log in again.")
        return redirect("login")

    if request.method != "POST":
        return render_verify(request, pending, purpose="login")

    valid, msg = verify_otp_code(pending["phone"], request.POST.get("otp", "").strip())
    if not valid:
        messages.error(request, msg)
        return render_verify(request, pending, purpose="login")

    request.session.pop("pending_login", None)
    user = User.objects.filter(username=pending["phone"], is_active=True).first()
    if user is None:
        return redirect("banned")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("habit_list")


@ratelimit(key="ip", rate="10/m", block=True)
def login_link_view(request, token):
    # GET shows a button and POST logs in, so link previews can't use up the link.
    user = user_for_login_link(token)
    if user is None:
        messages.error(request, "That login link has expired. Send /dashboard to the bot for a new one.")
        return redirect("login")
    if request.method == "POST":
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("habit_list")
    return render(request, "habits/login_link.html")


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------


@csrf_exempt
@require_POST
def telegram_webhook(request):
    secret = settings.TELEGRAM_WEBHOOK_SECRET
    sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secret or not hmac.compare_digest(sent, secret):
        return HttpResponseForbidden()
    try:
        update = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)
    try:
        handle_update(update)
    except Exception:
        # Always answer 200: Telegram retries failed updates, which would repeat them.
        logger.exception("Telegram update failed: %s", update.get("update_id"))
    return HttpResponse("ok")


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@login_required
def user_logout(request):
    if request.method == "POST":
        logout(request)
        messages.success(request, "You have been logged out.")
        return redirect("index")
    return redirect("habit_list")


def lockout_view(request, exception=None):
    return render(request, "habits/lockout.html", status=403)


# ---------------------------------------------------------------------------
# Habit list (dashboard)
# ---------------------------------------------------------------------------


@login_required
def habit_list(request):
    if banned_check(request.user):
        logout(request)
        return redirect("banned")

    today = timezone.localdate()
    now = timezone.localtime()
    hour = now.hour

    if 5 <= hour < 12:
        greeting = "Good morning"
    elif 12 <= hour < 17:
        greeting = "Good afternoon"
    elif 17 <= hour < 21:
        greeting = "Good evening"
    else:
        greeting = "Hey, night owl"

    habits = list(
        Habit.objects.filter(user=request.user).only(
            "id",
            "name",
            "category",
            "current_streak",
            "missed_count",
            "last_marked_date",
            "cached_nudge",
        )
    )

    total_streak = sum(h.current_streak for h in habits)
    max_misses = max((h.missed_count for h in habits), default=0)
    days_left = max(3 - max_misses, 0)

    return render(
        request,
        "habits/habit_list.html",
        {
            "habits": habits,
            "today": today,
            "total_streak": total_streak,
            "total_misses": max_misses,
            "max_days_left": days_left,
            "greeting": greeting,
        },
    )


# ---------------------------------------------------------------------------
# Add Habit
# ---------------------------------------------------------------------------


@login_required
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def add_habit(request):
    if banned_check(request.user):
        logger.warning(f"Banned user {request.user.id} attempted to add habit.")
        return redirect("banned")

    if request.method != "POST":
        return render(request, "habits/add_habit.html")

    habit_name, category = parse_habit(request.POST)
    if not habit_name:
        return HttpResponse("Invalid parameters.", status=400)

    try:
        with transaction.atomic():
            if Habit.objects.filter(
                user=request.user, name__iexact=habit_name
            ).exists():
                return HttpResponse("You're already working on this habit.", status=400)

            if Habit.objects.filter(user=request.user).count() >= 3:
                return HttpResponse("Max 3 habits. Focus is key.", status=400)

            habit = Habit.objects.create(
                user=request.user,
                name=habit_name,
                category=category,
            )

            if request.headers.get("HX-Request"):
                return HttpResponse(
                    status=204,
                    headers={"HX-Redirect": "/habits/"}
                )

    except Exception as e:
        logger.exception(f"Failure in add_habit for {request.user.id}")
        return HttpResponse("Server error. Try again.", status=500)

    return redirect("habit_list")


# ---------------------------------------------------------------------------
# Mark habit done
# ---------------------------------------------------------------------------


@login_required
@require_POST
@ratelimit(key="user", rate="15/m", method="POST", block=True)
def mark_habit_done(request, habit_id):
    if banned_check(request.user):
        logout(request)
        return JsonResponse({"status": "banned"}, status=403)

    try:
        habit = Habit.objects.get(id=habit_id, user=request.user)
    except Habit.DoesNotExist:
        return JsonResponse({"status": "not_found"}, status=404)

    result = habit.mark_done()

    total_streak = sum(
        h.current_streak for h in Habit.objects.filter(user=request.user)
    )
    result["total_streak"] = total_streak
    return JsonResponse(result)


# ---------------------------------------------------------------------------
# One-tap clock-in from a reminder link
# ---------------------------------------------------------------------------


@ratelimit(key="ip", rate="30/m", block=True)
def clock_in(request, token):
    # GET only shows the page: WhatsApp fetches links to build previews, so
    # opening the link must never clock anyone in. The button POSTs.
    user = user_for_clock_in(token)
    if user is None:
        return render(request, "habits/clock_in.html", {"expired": True}, status=410)
    if banned_check(user):
        return redirect("banned")

    if request.method == "POST":
        habit = Habit.objects.filter(id=request.POST.get("habit_id"), user=user).first()
        if habit is None:
            messages.error(request, "That habit wasn't found.")
        else:
            result = habit.mark_done()
            messages.success(
                request, f"{habit.name}: {result['current_streak']} day streak. Keep going."
            )
        return redirect("clock_in", token=token)

    habits = list(Habit.objects.filter(user=user).order_by("id"))
    if all(h.is_quit_habit for h in habits):
        question = "Stayed off it today?"
    elif all(not h.is_quit_habit and not h.is_custom_habit for h in habits):
        question = "Did it today?"
    else:
        question = "How did today go?"
    return render(
        request,
        "habits/clock_in.html",
        {
            "habits": habits,
            "all_done": all(h.marked_today for h in habits),
            "question": question,
        },
    )


# ---------------------------------------------------------------------------
# Banned page
# ---------------------------------------------------------------------------


def banned_view(request):
    return render(request, "habits/banned.html", status=403)


# ---------------------------------------------------------------------------
# Maintenance Trigger & Gemini Integration
# ---------------------------------------------------------------------------


@csrf_exempt
@require_POST
def maintenance_trigger(request):
    # Header, form field or ?key= in the URL (for schedulers that can't set
    # headers). Whitespace is stripped because keys get pasted with spaces.
    key = (
        request.headers.get("X-Maintenance-Key")
        or request.POST.get("key")
        or request.GET.get("key")
        or ""
    ).strip()
    if not key or not hmac.compare_digest(key, settings.MAINTENANCE_KEY):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    task = request.GET.get("task", "send_nudges")
    now = timezone.localtime()

    FALLBACK_NUDGES = [
        "No allow your fire go out. Stay focused.",
        "You don start, no go back now. Finish wetin you start.",
        "Every day you hold on, you dey win. Keep going.",
        "Your future self go thank you. Do it for am.",
        "The goal no go chase itself. You must move.",
    ]

    def pick_nudge(user_id: int) -> str:
        return FALLBACK_NUDGES[user_id % len(FALLBACK_NUDGES)]

    def active_habits_by_user():
        user_habits = defaultdict(list)
        for habit in Habit.objects.filter(user__is_active=True).select_related(
            "user__profile"
        ):
            user_habits[habit.user].append(habit)
        return user_habits

    if task == "send_nudges":
        hour = now.hour
        if 5 <= hour <= 11:
            task_type = "MORNING GINGER"
        elif 12 <= hour <= 17:
            task_type = "AFTERNOON PUSH"
        else:
            task_type = "DAILY CHECK-IN"

        user_habits = active_habits_by_user()
        sent = skipped = 0
        for user, habits in user_habits.items():
            if all(h.marked_today for h in habits):
                # Already done for today: don't spend a message on them.
                skipped += 1
                continue
            try:
                if send_reminder(user, f"*{task_type}*", habits, pick_nudge(user.id)):
                    sent += 1
            except Exception as e:
                logger.error("Send failed for user %s: %s", user.username, e)

        return HttpResponse(
            f"Sent to {sent}/{len(user_habits)} users. {skipped} already done."
        )

    if task == "night_watch":
        # The 9pm reminder, only to people still to clock in.
        # Misses are counted by close_day after 06:00, when the day has ended.
        user_habits = active_habits_by_user()
        sent = 0
        for user, habits in user_habits.items():
            if all(h.marked_today for h in habits):
                continue
            pending = [h.name for h in habits if not h.marked_today]
            if len(pending) > 1:
                pending_names = ", ".join(pending[:-1]) + " and " + pending[-1]
            else:
                pending_names = pending[0]
            try:
                note = (
                    f"{pick_nudge(user.id)}\n\n"
                    f"You haven't clocked in for {pending_names} today. "
                    "You have until 6am or it counts as a miss."
                )
                warnings = warning_lines(habits)
                if warnings:
                    note += f"\n\n{warnings}"
                if send_reminder(user, "*DAILY CHECK-IN*", habits, note):
                    sent += 1
            except Exception as e:
                logger.error("Night send failed for user %s: %s", user.username, e)

        return HttpResponse(f"Night watch done. Warned {sent} users.")

    if task == "close_day":
        result = close_day()
        for habit in result["lost"]:
            try:
                send_text(
                    habit.user,
                    f"💀 You lost your {habit.name} streak: 3 days missed in a row. "
                    "Clock in today to start it again. Lose every habit and you're out.",
                )
            except Exception as e:
                logger.error("Lost notice failed for user %s: %s", habit.user.username, e)
        for user in result["banned"]:
            try:
                send_text(
                    user,
                    "*YOU'RE OUT*\n\nEvery one of your habits is lost: 3 days missed "
                    "in a row. I warned you. Your DearSelf account is now closed.",
                )
            except Exception as e:
                logger.error("Ban notice failed for user %s: %s", user.username, e)

        return HttpResponse(
            f"Closed {result['day']}. {result['misses']} misses recorded. "
            f"{len(result['lost'])} streaks lost. {len(result['banned'])} banned."
        )

    if task == "whatsapp_status":
        return HttpResponse(whatsapp_status(), content_type="text/plain")

    if task == "debug_nudges":
        habits = Habit.objects.filter(user__is_active=True).select_related(
            "user__profile"
        )
        lines = [
            f"Habit: {h.name} | streak: {h.current_streak} | misses: {h.missed_count} | last_marked: {h.last_marked_date}"
            for h in habits
        ]
        return HttpResponse("\n".join(lines), content_type="text/plain")

    return HttpResponseForbidden(f"Unknown task: {task}")


def health_check(request):
    return HttpResponse("ok", status=200)