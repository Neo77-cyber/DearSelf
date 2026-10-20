from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.core.validators import RegexValidator


def get_logical_date():

    now = timezone.localtime()
    if now.hour < 6:
        return (now - timezone.timedelta(days=1)).date()
    return now.date()


class Profile(models.Model):
    phone_regex = RegexValidator(
        regex=r"^234\d{10}$",
        message="Phone number must be entered in the format: '2348123456789'.",
    )
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    # Telegram signups have no phone number; WhatsApp signups have no chat id.
    phone_number = models.CharField(
        validators=[phone_regex], max_length=13, unique=True, null=True, blank=True
    )
    telegram_chat_id = models.BigIntegerField(unique=True, null=True, blank=True)
    user_email = models.EmailField(unique=True, blank=True, null=True)
    timezone = models.CharField(max_length=50, default="Africa/Lagos")
    is_whatsapp_verified = models.BooleanField(default=False)

    def __str__(self):
        return f"Profile for {self.user.username}"


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        pass


class Habit(models.Model):
    HABIT_CHOICES = [
        ("DAILY PRAYERS", "Daily Prayers"),
        ("WORK OUT", "Work Out"),
        ("EXAM PREPARATION", "Exam Preparation"),
        ("GAMBLING", "Gambling"),
        ("WEED SOBER", "Weed Sober"),
        ("LATE NIGHT EATING", "Late Night Eating"),
        ("BUY BUY", "Buy Buy"),
        ("ALCOHOL SOBER", "Alcohol Sober"),
        ("CUSTOM", "Custom Habit"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="habits")
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=225, choices=HABIT_CHOICES, default="CUSTOM")
    goal = models.CharField(max_length=255, default="Daily")
    created_at = models.DateTimeField(auto_now_add=True)

    current_streak = models.PositiveIntegerField(default=0)
    longest_streak = models.PositiveIntegerField(default=0)
    missed_count = models.PositiveIntegerField(default=0)
    last_marked_date = models.DateField(null=True, blank=True)
    cached_nudge = models.TextField(null=True, blank=True)
    nudge_generated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "last_marked_date"]),
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.user.username})"

    # Habits you're quitting: "clocking in" means you stayed off it today.
    QUIT_CATEGORIES = {
        "GAMBLING",
        "WEED SOBER",
        "LATE NIGHT EATING",
        "BUY BUY",
        "ALCOHOL SOBER",
    }

    @property
    def is_quit_habit(self) -> bool:
        return self.category in self.QUIT_CATEGORIES

    @property
    def is_custom_habit(self) -> bool:
        return self.category == "CUSTOM"

    @property
    def clock_in_label(self) -> str:
        if self.is_custom_habit:
            return "Passed"
        return "I didn't do it" if self.is_quit_habit else "I did it"

    @property
    def marked_today(self) -> bool:
        return self.last_marked_date == get_logical_date()

    @property
    def missed_yesterday(self) -> bool:
        if self.last_marked_date is None:
            return False
        today = get_logical_date()
        yesterday = today - timezone.timedelta(days=1)
        return self.last_marked_date < yesterday

    def mark_done(self) -> dict:
        today = get_logical_date()

        if self.last_marked_date == today:
            return {"status": "already_done", "current_streak": self.current_streak}

        yesterday = today - timezone.timedelta(days=1)

        if self.last_marked_date == yesterday:
            self.current_streak += 1
        else:

            self.current_streak = 1

        if self.current_streak > self.longest_streak:
            self.longest_streak = self.current_streak

        self.last_marked_date = today
        # Misses count in a row: showing up resets them.
        self.missed_count = 0

        DailyRecord.objects.update_or_create(
            habit=self, date=today, defaults={"completed": True}
        )

        self.save(
            update_fields=[
                "current_streak",
                "longest_streak",
                "last_marked_date",
                "missed_count",
            ]
        )

        return {
            "status": "success",
            "current_streak": self.current_streak,
            "longest_streak": self.longest_streak,
        }

    def record_miss(self) -> dict:
        self.missed_count += 1
        self.current_streak = 0

        banned = False
        if self.missed_count >= 3:
            user = self.user
            user.is_active = False
            user.save(update_fields=["is_active"])
            banned = True

        self.save(update_fields=["missed_count", "current_streak"])

        return {
            "status": "banned" if banned else "missed",
            "missed_count": self.missed_count,
        }


class DailyRecord(models.Model):
    habit = models.ForeignKey(Habit, on_delete=models.CASCADE, related_name="records")
    date = models.DateField(default=timezone.now)
    completed = models.BooleanField(default=False)

    class Meta:
        unique_together = ["habit", "date"]

    def __str__(self):
        return f"{self.habit.name} on {self.date}"


class PhoneOTP(models.Model):
    # One live signup code per phone. Kept in the DB rather than the session so the
    # concurrent requests of a double tap can't overwrite or wipe each other's code.
    phone_number = models.CharField(max_length=13, unique=True)
    otp_hash = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField()

    def __str__(self):
        return f"OTP for {self.phone_number}"


class ServiceFlag(models.Model):
    # Small persistent switches shared by every worker, e.g. "WhatsApp can't
    # take new chats until <date>".
    name = models.CharField(max_length=50, unique=True)
    active_until = models.DateTimeField()

    def __str__(self):
        return f"{self.name} until {self.active_until}"
