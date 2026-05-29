import logging
import time
from collections import defaultdict
import hmac

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt


from django_ratelimit.decorators import ratelimit
from django_ratelimit.core import is_ratelimited


from .models import Habit, Profile
from .services.helpers import clean_phone_number, parse_habit, banned_check
from .services.otp import (
    generate_otp,
    send_otp,
    store_otp,
    verify_otp as verify_otp_code,
)
from .services.whatsapp import send_whatsapp_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@ratelimit(key="ip", rate="10/m", method="POST", block=True)
def index(request):
    if request.user.is_authenticated:
        return redirect("habit_list")

    if request.method != "POST":
        return render(request, "habits/index.html")

    raw_phone = request.POST.get("identifier", "").strip()
    clean_number = clean_phone_number(raw_phone)
    if not clean_number:
        messages.error(
            request,
            "Phone number is invalid. Please remove the '0' before your number (e.g. 8123 not 08123).",
        )
        return render(request, "habits/index.html")

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
        return render(request, "habits/index.html")

    password = request.POST.get("password", "")
    if not password:
        messages.error(request, "A password is required.")
        return render(request, "habits/index.html")

    try:
        validate_password(password, user=None)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return render(request, "habits/index.html")

    habit_name, category = parse_habit(request.POST)
    if not habit_name:
        messages.error(request, "Please select a valid habit.")
        return render(request, "habits/index.html")

    if User.objects.filter(username=clean_number).exists():
        messages.info(
            request, "An account with this number already exists. Please log in."
        )
        return redirect("login")

    request.session["pending_registration"] = {
        "phone": clean_number,
        "password": password,
        "habit_name": habit_name,
        "category": category,
    }
    request.session.modified = True
    request.session.save()

    otp = generate_otp()
    sent, _ = send_otp(clean_number, otp)

    if not sent:
        messages.error(
            request,
            "We couldn't reach that number on WhatsApp. Please try again.",
        )
        return render(request, "habits/index.html")

    store_otp(request, clean_number, otp)

    return redirect("verify_otp")


# --- verify_otp_view ---


@ratelimit(key="ip", rate="5/m", method="POST", block=True)
def verify_otp_view(request):

    if request.user.is_authenticated:
        return redirect("habit_list")

    pending = request.session.get("pending_registration")
    if not pending:
        logger.error(
            "No pending_registration in session. Session contents: %s",
            dict(request.session),
        )
        messages.error(request, "Session expired. Please register again.")
        return redirect("index")

    if request.method != "POST":
        return render(request, "habits/verify_otp.html")

    submitted_otp = request.POST.get("otp", "").strip()
    if not submitted_otp:
        messages.error(request, "Please enter the code we sent you.")
        return render(request, "habits/verify_otp.html")

    valid, msg = verify_otp_code(request, pending["phone"], submitted_otp)
    if not valid:
        messages.error(request, msg)
        return render(request, "habits/verify_otp.html")

    phone = pending["phone"]
    password = pending["password"]
    habit_name = pending["habit_name"]
    category = pending["category"]

    request.session.pop("pending_registration", None)
    request.session.save()

    if User.objects.filter(username=phone).exists():
        messages.info(
            request, "An account with this number already exists. Please log in."
        )
        return redirect("login")

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=phone,
                password=password,
            )
            Profile.objects.create(
                user=user,
                phone_number=phone,
                is_whatsapp_verified=True,
            )
            Habit.objects.create(
                user=user,
                name=habit_name,
                category=category,
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
        send_whatsapp_message(
            phone,
            "Welcome to DearSelf. I will remind you 3 times a day. "
            "If you miss 3 days streak you are out. Be warned.",
        )
    except Exception as e:
        logger.warning("Welcome message failed for %s: %s", phone[:4], e)

    return redirect("habit_list")


# --- resend_otp ---


@ratelimit(key="ip", rate="10/m", method="POST", block=True)
def resend_otp(request):
    pending = request.session.get("pending_registration")
    if not pending:
        messages.error(request, "Session expired. Please register again.")
        return redirect("index")

    otp = generate_otp()
    sent, _ = send_otp(pending["phone"], otp)

    if not sent:
        messages.error(request, "Failed to resend. Please try again.")
        return redirect("verify_otp")

    store_otp(request, pending["phone"], otp)
    messages.success(request, "A new code has been sent to your WhatsApp.")

    return redirect("verify_otp")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@ratelimit(key="ip", rate="10/m", method="POST", block=True)
def login_view(request):
    if request.user.is_authenticated:
        return redirect("habit_list")

    if request.method == "POST":
        raw_phone = request.POST.get("identifier", "").strip()
        password = request.POST.get("password", "")

        clean_number = clean_phone_number(raw_phone)
        if not clean_number:
            messages.error(request, "That phone number is not valid.")
            return render(request, "habits/login.html")

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
                "Too many failed login attempts for this number. Try again in an hour.",
            )
            return render(request, "habits/login.html")

        try:
            existing = User.objects.get(username=clean_number)
            if not existing.is_active:
                return redirect("banned")
        except User.DoesNotExist:
            pass

        user = authenticate(request, username=clean_number, password=password)
        if user is not None:
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect("habit_list")
        else:
            messages.error(request, "Invalid number or password.")

    return render(request, "habits/login.html")


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
    key = request.headers.get("X-Maintenance-Key") or request.POST.get("key")
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

    if task == "send_nudges":
        hour = now.hour
        if 5 <= hour <= 11:
            task_type = "MORNING GINGER"
        elif 12 <= hour <= 17:
            task_type = "AFTERNOON PUSH"
        else:
            task_type = "DAILY CHECK-IN"

        active_habits = list(
            Habit.objects.filter(user__is_active=True).select_related("user__profile")
        )

        user_habits = defaultdict(list)
        for habit in active_habits:
            user_habits[habit.user].append(habit)

        sent = 0
        for user, habits in user_habits.items():
            try:
                nudge = pick_nudge(user.id)

                habit_lines = "\n".join(
                    f"• {h.name}: {h.current_streak} day streak | {h.missed_count}/3 misses"
                    for h in habits
                )

                wa_message = (
                    f"*{task_type}*\n\n"
                    f"{habit_lines}\n\n"
                    f"{nudge}\n\n"
                    f"Go to your browser to clock in\nhttps://dear-self.onrender.com/habits/"
                )
                send_whatsapp_message(user.profile.phone_number, wa_message)
                sent += 1
            except Exception as e:
                logger.error("Send failed for user %s: %s", user.username, e)

        return HttpResponse(f"Sent to {sent}/{len(user_habits)} users.")

    if task == "night_watch":
        now = timezone.localtime()
        if now.hour < 6:
            logical_today = (now - timezone.timedelta(days=1)).date()
        else:
            logical_today = now.date()

        stale_habits = list(
            Habit.objects.filter(
                user__is_active=True, last_marked_date__lt=logical_today
            )
            .exclude(last_marked_date=None)
            .select_related("user__profile")
        )

        banned_users = set()
        banned_count = 0
        for habit in stale_habits:
            result = habit.record_miss()
            if result["status"] == "banned":
                banned_count += 1
                banned_users.add(habit.user_id)
                logger.warning("User %s banned after 3 misses.", habit.user.username)

        active_habits = list(
            Habit.objects.filter(user__is_active=True).select_related("user__profile")
        )

        user_habits = defaultdict(list)
        for habit in active_habits:
            user_habits[habit.user].append(habit)

        sent = 0
        for user, habits in user_habits.items():
            try:
                nudge = pick_nudge(user.id)

                habit_lines = "\n".join(
                    f"• {h.name}: {h.current_streak} day streak | {h.missed_count}/3 misses"
                    for h in habits
                )

                wa_message = (
                    "*NIGHT WATCH*\n\n"
                    f"{habit_lines}\n\n"
                    f"{nudge}\n\n"
                    "https://dear-self.onrender.com/habits/"
                )
                send_whatsapp_message(user.profile.phone_number, wa_message)
                sent += 1
            except Exception as e:
                logger.error("Night send failed for user %s: %s", user.username, e)

        return HttpResponse(
            f"Night watch done. {len(stale_habits)} misses recorded. "
            f"{banned_count} banned. {sent}/{len(user_habits)} messages sent."
        )

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